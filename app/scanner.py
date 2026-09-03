from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path
from queue import Queue, Empty

from PIL import Image, UnidentifiedImageError
from .config import load_config
from .db import connect

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".avif", ".gif"}

PATTERN_C = re.compile(
    r"^(.+?)[-_ ]+(第?\d+[卷册部])[-_ ]+(第?\d+[话話回]?.*)$"
    r"|^(.+?)[-_ ]+(第?\d+[话話回卷].*)$"
)


def natural_key(s: str):
    return [int(x) if x.isdigit() else x.lower() for x in re.split(r"(\d+)", str(s))]


def is_image(name: str) -> bool:
    name_l = str(name).lower()
    return any(name_l.endswith(ext) for ext in IMAGE_EXTS)


class ScanManager:
    """
    全自动层级化漫画扫描器：
    - 后台仅需一个漫画库根目录（例如 D:\\漫画库 或 /home/leaf/D/.漫画）
    - 自动将第一级子目录作为来源站点（网站A、网站B、网站C、noyacg、b...）
    - 自动识别 Type A (单本)、Type B (卷/话分层)、Type C (连字符命名)
    - 数据库存储相对路径，毫秒级批量事务入库
    - 全局实时进度跟踪
    """
    def __init__(self) -> None:
        self.queue: Queue[tuple[str, int | None]] = Queue()
        self.stop_event = threading.Event()
        self.worker_thread = threading.Thread(target=self._worker, daemon=True)
        self.poller_thread = threading.Thread(target=self._poller, daemon=True)
        self.source_state: dict[str, float] = {}
        self.progress: dict = {
            "running": False,
            "source_name": "",
            "total_mangas": 0,
            "current_manga": 0,
            "percent": 0.0,
            "current_title": "",
            "message": "空闲",
            "updated_at": time.time(),
        }

    def start(self) -> None:
        if not self.worker_thread.is_alive():
            self.worker_thread.start()
        if not self.poller_thread.is_alive():
            self.poller_thread.start()

    def stop(self) -> None:
        self.stop_event.set()

    def get_progress(self) -> dict:
        p = dict(self.progress)
        p["paused"] = getattr(self, "paused", False)
        return p

    def pause(self) -> None:
        self.paused = True
        self.progress["message"] = "已暂停扫描"

    def resume(self) -> None:
        self.paused = False
        self.progress["message"] = "恢复扫描中"

    def enqueue_full_scan(self) -> None:
        self.queue.put(("full", None))

    def enqueue_source_scan(self, source_id: int) -> None:
        self.queue.put(("source", source_id))

    def _poller(self) -> None:
        while not self.stop_event.is_set():
            time.sleep(20)
            if getattr(self, "paused", False):
                continue
            cfg = load_config()
            root_str = cfg.get("library_root", "")
            if not root_str:
                continue
            root = Path(root_str)
            if not root.is_dir():
                continue
            try:
                mtime = root.stat().st_mtime
                old_mtime = self.source_state.get("__root__")
                if old_mtime is None or mtime > old_mtime:
                    self.source_state["__root__"] = mtime
                    self.enqueue_full_scan()
            except OSError:
                continue

    def _worker(self) -> None:
        while not self.stop_event.is_set():
            if getattr(self, "paused", False):
                time.sleep(0.5)
                continue
            try:
                kind, sid = self.queue.get(timeout=0.5)
            except Empty:
                continue
            try:
                if kind == "full":
                    self.scan_all()
                elif kind == "source" and sid:
                    self.scan_single_source(sid)
            except Exception as exc:
                print(f"[scanner] Scan worker error: {exc}", flush=True)
            finally:
                self.queue.task_done()

    def scan_all(self) -> None:
        cfg = load_config()
        root_str = cfg.get("library_root", "")
        if not root_str:
            return
        root = Path(root_str)
        if not root.is_dir():
            self.progress.update({
                "running": False,
                "message": f"根目录不存在: {root_str}",
                "updated_at": time.time()
            })
            return

        print(f"[scanner] Full library scan started: {root}", flush=True)
        self.progress.update({
            "running": True,
            "source_name": "全库扫描",
            "total_mangas": 0,
            "current_manga": 0,
            "percent": 0.0,
            "current_title": "正在检索来源站点...",
            "message": "正在检索第一级子站点...",
            "updated_at": time.time()
        })

        # 检索第一级子目录作为站点/来源
        direct_subdirs = [p for p in root.iterdir() if p.is_dir() and not p.name.startswith('.')]
        direct_images = [p for p in root.iterdir() if p.is_file() and is_image(p.name)]

        con = connect()
        try:
            cur = con.execute("INSERT INTO scan_jobs(status, started_at) VALUES(?, CURRENT_TIMESTAMP)", ("running",))
            job_id = int(cur.lastrowid)
            con.commit()
        except Exception:
            job_id = None
        finally:
            con.close()

        if direct_subdirs and not direct_images:
            sites_to_scan = [(p.name, p) for p in sorted(direct_subdirs, key=lambda p: natural_key(p.name))]
        else:
            sites_to_scan = [(root.name or "主库", root)]

        active_source_ids = []
        total_mangas_all = 0

        for site_name, site_path in sites_to_scan:
            con = connect()
            try:
                rel_path = site_name if site_path != root else "."
                row = con.execute("SELECT id FROM sources WHERE rel_path=?", (rel_path,)).fetchone()
                if not row:
                    cur = con.execute("INSERT INTO sources(name, rel_path, enabled) VALUES(?, ?, 1)", (site_name, rel_path))
                    sid = int(cur.lastrowid)
                    con.commit()
                else:
                    sid = int(row["id"])
                    con.execute("UPDATE sources SET name=? WHERE id=?", (site_name, sid))
                    con.commit()
                active_source_ids.append(sid)
            finally:
                con.close()

            # 扫描该站点
            count = self._scan_site(sid, site_name, site_path)
            total_mangas_all += count

        # 清理已在磁盘上移除的站点
        con = connect()
        try:
            if active_source_ids:
                placeholders = ",".join("?" * len(active_source_ids))
                con.execute(f"DELETE FROM sources WHERE id NOT IN ({placeholders})", active_source_ids)
                con.commit()
            if job_id:
                con.execute("UPDATE scan_jobs SET status='success', message=?, finished_at=CURRENT_TIMESTAMP WHERE id=?",
                            (f"全库扫描完成，共 {total_mangas_all} 部漫画", job_id))
                con.commit()
        except Exception as e:
            print(f"[scanner] Cleanup error: {e}", flush=True)
        finally:
            con.close()

        self.progress.update({
            "running": False,
            "percent": 100.0,
            "current_manga": total_mangas_all,
            "total_mangas": total_mangas_all,
            "current_title": "",
            "message": f"全库扫描完成！共发现 {len(sites_to_scan)} 个站点，{total_mangas_all} 部漫画",
            "updated_at": time.time()
        })
        print(f"[scanner] Full scan finished! Total mangas: {total_mangas_all}", flush=True)

    def scan_single_source(self, source_id: int) -> None:
        cfg = load_config()
        root = Path(cfg.get("library_root", ""))
        con = connect()
        try:
            source = con.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        finally:
            con.close()
        if not source:
            return
        site_path = root if source["rel_path"] == "." else root / source["rel_path"]
        if site_path.is_dir():
            self._scan_site(source_id, source["name"], site_path)

    def _scan_site(self, source_id: int, site_name: str, site_path: Path) -> int:
        print(f"[scanner] Scanning site [{site_name}]: {site_path}", flush=True)
        found_folders: list[tuple[Path, list[str]]] = []
        try:
            for dirpath, dirnames, filenames in os.walk(site_path, followlinks=True):
                dirnames[:] = [d for d in dirnames if not d.startswith('.')]
                img_files = [f for f in filenames if is_image(f) and not f.startswith('.')]
                if img_files:
                    found_folders.append((Path(dirpath), img_files))
        except Exception as e:
            print(f"[scanner] Error walking {site_path}: {e}", flush=True)

        # 智能漫画结构聚类 (Type A, Type B, Type C)
        # 结构：mangas_dict[manga_key] = { "title": str, "rel_path": str, "chapters": [ { "title": str, "volume": str, "chapter_num": str, "rel_path": str, "img_files": list, "folder": Path } ] }
        mangas_dict: dict[str, dict] = {}

        for folder, img_files in found_folders:
            img_files.sort(key=natural_key)
            if not img_files:
                continue

            try:
                rel = folder.relative_to(site_path)
                parts = rel.parts
            except Exception:
                parts = (folder.name,)

            # 类型判断：
            if len(parts) == 1:
                folder_name = parts[0]
                # 尝试 Type C 连字符正则解析
                m = PATTERN_C.match(folder_name)
                if m:
                    g = [x for x in m.groups() if x is not None]
                    manga_title = g[0]
                    vol = g[1] if len(g) > 2 else ""
                    ch_num = g[2] if len(g) > 2 else g[1]
                    ch_title = f"{vol} {ch_num}".strip()
                    manga_key = manga_title
                    manga_rel = manga_title
                    ch_rel = folder_name
                else:
                    # Type A: 单本漫画
                    manga_title = folder_name
                    manga_key = folder_name
                    manga_rel = folder_name
                    ch_title = "全一话"
                    vol = ""
                    ch_num = "1"
                    ch_rel = "."
            elif len(parts) == 2:
                # Type B: 漫画名 / 第01话
                manga_title = parts[0]
                manga_key = parts[0]
                manga_rel = parts[0]
                ch_title = parts[1]
                vol = ""
                ch_num = parts[1]
                ch_rel = parts[1]
            elif len(parts) == 3:
                # Type B: 漫画名 / 第01卷 / 第01话
                manga_title = parts[0]
                manga_key = parts[0]
                manga_rel = parts[0]
                vol = parts[1]
                ch_num = parts[2]
                ch_title = f"{vol} · {ch_num}"
                ch_rel = f"{vol}/{ch_num}"
            else:
                # 深度嵌套
                manga_title = parts[0]
                manga_key = parts[0]
                manga_rel = parts[0]
                vol = parts[1]
                ch_num = parts[-1]
                ch_title = " · ".join(parts[1:])
                ch_rel = "/".join(parts[1:])

            if manga_key not in mangas_dict:
                mangas_dict[manga_key] = {
                    "title": manga_title,
                    "rel_path": manga_rel,
                    "chapters": []
                }

            mangas_dict[manga_key]["chapters"].append({
                "title": ch_title,
                "volume": vol,
                "chapter_num": ch_num,
                "rel_path": ch_rel,
                "img_files": img_files,
                "folder": folder,
            })

        total_mangas = len(mangas_dict)
        self.progress.update({
            "total_mangas": total_mangas,
            "current_manga": 0,
            "percent": 0.0,
            "message": f"[{site_name}] 发现 {total_mangas} 部漫画，正在批量入库...",
            "updated_at": time.time()
        })

        # 单连接批量事务入库
        con = connect()
        con.execute("PRAGMA synchronous = NORMAL")
        con.execute("PRAGMA cache_size = -64000")

        existing_manga = {r["rel_path"]: r["id"] for r in con.execute("SELECT id, rel_path FROM manga WHERE source_id=?", (source_id,)).fetchall()}
        scanned_manga_rels = set()
        count = 0

        try:
            for idx, (manga_key, m_data) in enumerate(mangas_dict.items(), start=1):
                m_title = m_data["title"]
                m_rel = m_data["rel_path"]
                scanned_manga_rels.add(m_rel)

                # 排序章节
                chapters = m_data["chapters"]
                chapters.sort(key=lambda c: natural_key(c["title"]))

                manga_id = existing_manga.get(m_rel)
                if not manga_id:
                    cur = con.execute("INSERT INTO manga(source_id, title, rel_path, chapter_count, page_count) VALUES(?,?,?,?,?)",
                                      (source_id, m_title, m_rel, len(chapters), 0))
                    manga_id = int(cur.lastrowid)
                    existing_manga[m_rel] = manga_id

                total_pages_in_manga = 0
                cover_rel = None

                # 加载现有 chapters
                existing_chapters = {r["rel_path"]: r["id"] for r in con.execute("SELECT id, rel_path FROM chapters WHERE manga_id=?", (manga_id,)).fetchall()}
                scanned_ch_rels = set()

                for order_idx, ch in enumerate(chapters, start=1):
                    ch_rel = ch["rel_path"]
                    scanned_ch_rels.add(ch_rel)
                    img_files = ch["img_files"]
                    ch_folder = ch["folder"]

                    if cover_rel is None and img_files:
                        # 封面相对路径：ch_rel + "/" + img_files[0]
                        if ch_rel == ".":
                            cover_rel = img_files[0]
                        else:
                            cover_rel = f"{ch_rel}/{img_files[0]}"

                    ch_id = existing_chapters.get(ch_rel)
                    if not ch_id:
                        cur = con.execute("INSERT INTO chapters(manga_id, title, volume, chapter_num, rel_path, page_count, order_num) VALUES(?,?,?,?,?,?,?)",
                                          (manga_id, ch["title"], ch["volume"], ch["chapter_num"], ch_rel, len(img_files), order_idx))
                        ch_id = int(cur.lastrowid)
                        existing_chapters[ch_rel] = ch_id
                    else:
                        con.execute("UPDATE chapters SET title=?, volume=?, chapter_num=?, page_count=?, order_num=? WHERE id=?",
                                    (ch["title"], ch["volume"], ch["chapter_num"], len(img_files), order_idx, ch_id))

                    total_pages_in_manga += len(img_files)

                    # 批量插入 pages
                    page_records = []
                    for p_num, img_name in enumerate(img_files, start=1):
                        img_path = ch_folder / img_name
                        stat = img_path.stat()
                        fmt = img_name.rsplit('.', 1)[-1].lower() if '.' in img_name else ''
                        p_rel = img_name if ch_rel == "." else f"{ch_rel}/{img_name}"
                        page_records.append((ch_id, manga_id, p_num, p_rel, stat.st_size, stat.st_mtime_ns, fmt))

                    con.executemany("""
                        INSERT INTO pages(chapter_id, manga_id, page_number, rel_path, file_size, mtime_ns, format)
                        VALUES(?,?,?,?,?,?,?)
                        ON CONFLICT(chapter_id, page_number) DO UPDATE SET
                          rel_path=excluded.rel_path, file_size=excluded.file_size, mtime_ns=excluded.mtime_ns, format=excluded.format
                    """, page_records)

                # 清理已删除的章节
                for old_ch_rel, old_ch_id in existing_chapters.items():
                    if old_ch_rel not in scanned_ch_rels:
                        con.execute("DELETE FROM chapters WHERE id=?", (old_ch_id,))

                # 更新漫画总页数、章节数、封面
                con.execute("UPDATE manga SET title=?, cover_rel_path=?, chapter_count=?, page_count=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                            (m_title, cover_rel, len(chapters), total_pages_in_manga, manga_id))

                count += 1
                if idx % 50 == 0 or idx == total_mangas:
                    con.commit()
                    pct = round((idx / total_mangas) * 100, 1) if total_mangas > 0 else 100.0
                    self.progress.update({
                        "current_manga": idx,
                        "percent": pct,
                        "current_title": m_title,
                        "message": f"[{site_name}] 正在索引 ({idx}/{total_mangas})",
                        "updated_at": time.time()
                    })

            # 清理已删除的漫画
            for old_m_rel, old_m_id in existing_manga.items():
                if old_m_rel not in scanned_manga_rels:
                    con.execute("DELETE FROM manga WHERE id=?", (old_m_id,))

            con.commit()
        except Exception as e:
            print(f"[scanner] Error scanning site {site_name}: {e}", flush=True)
        finally:
            con.close()

        print(f"[scanner] Site [{site_name}] done: {count} mangas.", flush=True)
        return count
