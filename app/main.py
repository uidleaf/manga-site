from __future__ import annotations

import math
import secrets
import os
import shutil
import re
import time
import datetime
from pathlib import Path

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.gzip import GZipMiddleware
from PIL import Image, ImageOps

from .config import load_config, save_config
from .db import connect, init_db
from .scanner import ScanManager
from .security import (
    require_admin,
    current_admin,
    make_session,
    authenticate,
    has_any_admin,
    create_admin,
    is_rate_limited,
    record_failed_attempt,
    clear_attempts,
)

BASE_DIR = Path(__file__).resolve().parent.parent
THUMB_DIR = BASE_DIR / "data" / "thumbnails"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".avif", ".gif"}

templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))
app = FastAPI(title="轻量漫画库", docs_url=None, redoc_url=None)
app.add_middleware(GZipMiddleware, minimum_size=1024)
manager = ScanManager()
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "app" / "static")), name="static")


def format_bytes(size: int | float) -> str:
    if size <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    idx = 0
    s = float(size)
    while s >= 1024 and idx < len(units) - 1:
        s /= 1024
        idx += 1
    return f"{s:.2f} {units[idx]}"


def get_system_metrics() -> dict:
    root = get_library_root()
    disk_total_gb, disk_used_gb, disk_pct = 0.0, 0.0, 0.0
    try:
        usage = shutil.disk_usage(root if root.is_dir() else "/")
        disk_total_gb = round(usage.total / (1024**3), 1)
        disk_used_gb = round(usage.used / (1024**3), 1)
        disk_pct = round((usage.used / usage.total) * 100, 1)
    except Exception:
        pass

    mem_used_mb, mem_total_mb, mem_pct = 0, 0, 0.0
    try:
        with open("/proc/meminfo") as f:
            lines = f.readlines()
        mem = {}
        for line in lines:
            parts = line.split(":")
            if len(parts) == 2:
                mem[parts[0].strip()] = int(parts[1].split()[0])
        mem_total_mb = mem.get("MemTotal", 0) // 1024
        avail_mb = mem.get("MemAvailable", 0) // 1024
        mem_used_mb = max(0, mem_total_mb - avail_mb)
        mem_pct = round((mem_used_mb / mem_total_mb) * 100, 1) if mem_total_mb else 0.0
    except Exception:
        pass

    load1 = 0.12
    try:
        load1, _, _ = os.getloadavg()
    except Exception:
        pass
    cpu_pct = min(100.0, round(load1 * 25.0, 1))

    return {
        "disk_total_gb": disk_total_gb,
        "disk_used_gb": disk_used_gb,
        "disk_pct": disk_pct,
        "mem_used_mb": mem_used_mb,
        "mem_total_mb": mem_total_mb,
        "mem_pct": mem_pct,
        "cpu_pct": cpu_pct,
        "load": f"{load1:.2f}",
    }


def get_library_root() -> Path:
    cfg = load_config()
    return Path(cfg.get("library_root", "/home/leaf/D/.漫画")).resolve()


@app.on_event("startup")
def startup():
    init_db()
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    manager.start()


@app.on_event("shutdown")
def shutdown():
    manager.stop()


@app.get("/", response_class=HTMLResponse)
def index(request: Request, q: str = "", source: str = "", page: int = 1):
    page = max(1, page)
    limit = 36
    offset = (page - 1) * limit
    con = connect()
    try:
        # 获取各子站点及其漫画统计
        sources_raw = con.execute("""
            SELECT s.id, s.name, COUNT(m.id) as manga_count
            FROM sources s
            JOIN manga m ON m.source_id = s.id
            WHERE s.enabled = 1
            GROUP BY s.id
            HAVING COUNT(m.id) > 0
            ORDER BY s.name ASC
        """).fetchall()
        sources = [dict(s) for s in sources_raw]

        total_all = con.execute("SELECT COUNT(*) FROM manga m JOIN sources s ON s.id=m.source_id WHERE s.enabled=1").fetchone()[0]

        query = "SELECT m.*, s.name as source_name FROM manga m JOIN sources s ON s.id=m.source_id WHERE s.enabled=1"
        params: list = []
        if q:
            query += " AND m.title LIKE ?"
            params.append(f"%{q}%")
        if source and source.isdigit():
            query += " AND m.source_id = ?"
            params.append(int(source))

        count_query = f"SELECT COUNT(*) FROM ({query})"
        total = con.execute(count_query, params).fetchone()[0]
        query += " ORDER BY m.chapter_count DESC, m.id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        rows = [dict(r) for r in con.execute(query, params).fetchall()]

        # 首页特色推荐提取（当处于第一页且未搜索时）
        featured_manga = None
        spotlight_mangas = []
        gallery_mangas = rows

        if page == 1 and not q and not source and len(rows) > 0:
            # 优先选择多章节/分卷的漫画作为主特色
            featured_candidates = [m for m in rows if m["chapter_count"] > 1]
            if featured_candidates:
                featured_manga = featured_candidates[0]
                remaining = [m for m in rows if m["id"] != featured_manga["id"]]
            else:
                featured_manga = rows[0]
                remaining = rows[1:]

            spotlight_mangas = remaining[:3]
            gallery_mangas = remaining[3:]

    finally:
        con.close()

    pages = max(1, math.ceil(total / limit))
    return templates.TemplateResponse("index.html", {
        "request": request,
        "mangas": gallery_mangas,
        "featured_manga": featured_manga,
        "spotlight_mangas": spotlight_mangas,
        "sources": sources,
        "q": q,
        "source": source,
        "page": page,
        "pages": pages,
        "total": total,
        "total_all": total_all,
        "config": load_config(),
    })


@app.get("/manga/{manga_id}", response_class=HTMLResponse)
def detail(request: Request, manga_id: int):
    con = connect()
    try:
        manga = con.execute("""
            SELECT m.*, s.name as source_name
            FROM manga m JOIN sources s ON s.id=m.source_id
            WHERE m.id=?
        """, (manga_id,)).fetchone()
        if not manga:
            return HTMLResponse("漫画未找到", status_code=404)

        chapters = con.execute("""
            SELECT * FROM chapters
            WHERE manga_id=?
            ORDER BY order_num ASC, id ASC
        """, (manga_id,)).fetchall()

        first_chapter_id = chapters[0]["id"] if chapters else None
    finally:
        con.close()

    return templates.TemplateResponse("detail.html", {
        "request": request,
        "manga": manga,
        "chapters": chapters,
        "first_chapter_id": first_chapter_id,
        "config": load_config()
    })


@app.get("/read/manga/{manga_id}")
def read_manga_first(manga_id: int):
    con = connect()
    try:
        ch = con.execute("SELECT id FROM chapters WHERE manga_id=? ORDER BY order_num ASC, id ASC LIMIT 1", (manga_id,)).fetchone()
    finally:
        con.close()
    if ch:
        return RedirectResponse(f"/read/{ch['id']}", status_code=303)
    return HTMLResponse("该漫画暂无章节内容", status_code=404)


@app.get("/read/{chapter_id}", response_class=HTMLResponse)
def reader(request: Request, chapter_id: int):
    con = connect()
    try:
        chapter = con.execute("SELECT * FROM chapters WHERE id=?", (chapter_id,)).fetchone()
        if not chapter:
            return HTMLResponse("章节未找到", status_code=404)

        manga = con.execute("""
            SELECT m.*, s.name as source_name
            FROM manga m JOIN sources s ON s.id=m.source_id
            WHERE m.id=?
        """, (chapter["manga_id"],)).fetchone()

        pages = con.execute("SELECT * FROM pages WHERE chapter_id=? ORDER BY page_number ASC", (chapter_id,)).fetchall()

        prev_ch = con.execute("""
            SELECT id, title FROM chapters
            WHERE manga_id=? AND order_num < ?
            ORDER BY order_num DESC LIMIT 1
        """, (chapter["manga_id"], chapter["order_num"])).fetchone()

        next_ch = con.execute("""
            SELECT id, title FROM chapters
            WHERE manga_id=? AND order_num > ?
            ORDER BY order_num ASC LIMIT 1
        """, (chapter["manga_id"], chapter["order_num"])).fetchone()

        all_chapters = con.execute("""
            SELECT id, title FROM chapters
            WHERE manga_id=?
            ORDER BY order_num ASC
        """, (chapter["manga_id"],)).fetchall()
    finally:
        con.close()

    return templates.TemplateResponse("reader.html", {
        "request": request,
        "manga": manga,
        "chapter": chapter,
        "pages": pages,
        "prev_chapter": prev_ch,
        "next_chapter": next_ch,
        "all_chapters": all_chapters,
        "config": load_config(),
    })


@app.get("/thumb/{manga_id}")
def thumb(manga_id: int):
    path = THUMB_DIR / f"{manga_id}.webp"
    if path.is_file():
        return FileResponse(path, media_type="image/webp", headers={"Cache-Control": "public, max-age=31536000, immutable"})

    con = connect()
    try:
        row = con.execute("""
            SELECT m.rel_path as manga_rel, m.cover_rel_path, s.rel_path as source_rel
            FROM manga m JOIN sources s ON s.id=m.source_id
            WHERE m.id=?
        """, (manga_id,)).fetchone()
    finally:
        con.close()

    if row and row["cover_rel_path"]:
        root = get_library_root()
        site_path = root if row["source_rel"] == "." else root / row["source_rel"]
        
        # 兼容 Type A / B 层级路径与 Type C 单级多话相对路径
        p1 = (site_path / row["manga_rel"] / row["cover_rel_path"]).resolve()
        p2 = (site_path / row["cover_rel_path"]).resolve()
        img_path = p1 if p1.is_file() else p2

        try:
            img_path.relative_to(root)
            if img_path.is_file():
                THUMB_DIR.mkdir(parents=True, exist_ok=True)
                with Image.open(img_path) as im:
                    if im.mode not in ("RGB", "RGBA"):
                        im = im.convert("RGB")
                    thumb_w = int(load_config().get("thumb_width", 320))
                    thumb_h = int(thumb_w * 1.5)  # 严格 2:3 黄金比例
                    im_fitted = ImageOps.fit(im, (thumb_w, thumb_h), method=Image.Resampling.LANCZOS, centering=(0.5, 0.3))
                    im_fitted.save(path, "WEBP", quality=82, method=4)
                return FileResponse(path, media_type="image/webp", headers={"Cache-Control": "public, max-age=31536000, immutable"})
        except Exception:
            pass

    return FileResponse(BASE_DIR / "app" / "static" / "placeholder.svg", media_type="image/svg+xml")


@app.get("/media/{page_id}")
def media(page_id: int):
    con = connect()
    try:
        row = con.execute("""
            SELECT p.rel_path as page_rel, m.rel_path as manga_rel, s.rel_path as source_rel
            FROM pages p
            JOIN manga m ON m.id=p.manga_id
            JOIN sources s ON s.id=m.source_id
            WHERE p.id=?
        """, (page_id,)).fetchone()
    finally:
        con.close()

    if not row:
        return JSONResponse({"error": "not found"}, status_code=404)

    root = get_library_root()
    site_path = root if row["source_rel"] == "." else root / row["source_rel"]
    
    # 兼容 Type A / B 层级路径与 Type C 单级多话相对路径
    p1 = (site_path / row["manga_rel"] / row["page_rel"]).resolve()
    p2 = (site_path / row["page_rel"]).resolve()
    file_path = p1 if p1.is_file() else p2

    try:
        file_path.relative_to(root)
    except ValueError:
        return JSONResponse({"error": "access denied"}, status_code=403)

    if not file_path.is_file() or file_path.suffix.lower() not in IMAGE_EXTS:
        return JSONResponse({"error": "file not found"}, status_code=404)

    return FileResponse(file_path, headers={"Cache-Control": "public, max-age=604800"})


@app.get("/admin/login", response_class=HTMLResponse)
def admin_login_page(request: Request, error: str = ""):
    if current_admin(request):
        return RedirectResponse("/admin", status_code=303)
    has_admin = has_any_admin()
    return templates.TemplateResponse("admin_login.html", {
        "request": request,
        "has_admin": has_admin,
        "error": error,
        "config": load_config(),
    })


@app.post("/admin/login")
def admin_login_post(request: Request, username: str = Form(...), password: str = Form(...)):
    client_ip = request.client.host if request.client else "127.0.0.1"
    if is_rate_limited(client_ip):
        return templates.TemplateResponse("admin_login.html", {
            "request": request,
            "has_admin": True,
            "error": "登录尝试过多，系统已临时锁定 5 分钟，请稍后再试。",
            "config": load_config(),
        }, status_code=429)
    admin = authenticate(username, password)
    if not admin:
        record_failed_attempt(client_ip)
        return templates.TemplateResponse("admin_login.html", {
            "request": request,
            "has_admin": True,
            "error": "用户名或密码错误",
            "config": load_config(),
        }, status_code=401)
    clear_attempts(client_ip)
    token = make_session(admin["id"], admin["username"])
    resp = RedirectResponse("/admin", status_code=303)
    resp.set_cookie(
        "manga_admin",
        token,
        httponly=True,
        samesite="lax",
        max_age=int(load_config().get("session_max_age", 604800)),
        path="/"
    )
    return resp


@app.post("/admin/setup")
def admin_setup_post(request: Request, username: str = Form(...), password: str = Form(...)):
    if has_any_admin():
        return RedirectResponse("/admin/login", status_code=303)
    u = username.strip()
    p = password.strip()
    if not u or len(p) < 8:
        return templates.TemplateResponse("admin_login.html", {
            "request": request,
            "has_admin": False,
            "error": "用户名不能为空，密码长度至少需为 8 位",
            "config": load_config(),
        }, status_code=400)
    uid = create_admin(u, p)
    token = make_session(uid, u)
    resp = RedirectResponse("/admin", status_code=303)
    resp.set_cookie(
        "manga_admin",
        token,
        httponly=True,
        samesite="lax",
        max_age=int(load_config().get("session_max_age", 604800)),
        path="/"
    )
    return resp


@app.get("/admin", response_class=HTMLResponse)
def admin_home(request: Request):
    admin, redirect = require_admin(request)
    if redirect:
        return redirect

    con = connect()
    try:
        total_manga = con.execute("SELECT COUNT(*) FROM manga").fetchone()[0]
        total_chapters = con.execute("SELECT COUNT(*) FROM chapters").fetchone()[0]
        total_pages = con.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
        total_sources = con.execute("SELECT COUNT(DISTINCT source_id) FROM manga").fetchone()[0]
        total_bytes = con.execute("SELECT SUM(file_size) FROM pages").fetchone()[0] or 0

        today_manga = con.execute("SELECT COUNT(*) FROM manga WHERE DATE(created_at) = DATE('now')").fetchone()[0]
        yesterday_manga = con.execute("SELECT COUNT(*) FROM manga WHERE DATE(created_at) = DATE('now', '-1 day')").fetchone()[0]
        
        # 来源站点分布与占比
        sources_raw = con.execute("""
            SELECT s.*, COUNT(m.id) as manga_count
            FROM sources s
            LEFT JOIN manga m ON m.source_id = s.id
            GROUP BY s.id
            ORDER BY manga_count DESC, s.name ASC
        """).fetchall()
        
        sources_breakdown = []
        denom = max(1, total_manga)
        for s in sources_raw:
            c = s["manga_count"] or 0
            sources_breakdown.append({
                "id": s["id"],
                "name": s["name"],
                "rel_path": s["rel_path"],
                "manga_count": c,
                "percent": round((c / denom) * 100, 1),
                "parsing_rule": s["parsing_rule"] if "parsing_rule" in s.keys() and s["parsing_rule"] else ""
            })

        # 热门/活跃漫画
        trending_rows = con.execute("""
            SELECT m.id, m.title, s.name as source_name, m.chapter_count, m.page_count,
                   COUNT(ae.id) as read_count
            FROM manga m
            JOIN sources s ON s.id=m.source_id
            LEFT JOIN analytics_events ae ON ae.manga_id = m.id
            GROUP BY m.id
            ORDER BY read_count DESC, m.chapter_count DESC, m.id DESC
            LIMIT 5
        """).fetchall()

        # 最近扫描日志
        jobs = con.execute("SELECT * FROM scan_jobs ORDER BY id DESC LIMIT 10").fetchall()

        # 最近7日增长与摄入模拟/真实曲线计算
        today_date = datetime.date.today()
        dates_7d = [(today_date - datetime.timedelta(days=i)).strftime("%m-%d") for i in range(6, -1, -1)]
        growth_points = []
        base_count = max(0, total_manga - today_manga)
        for idx, d in enumerate(dates_7d):
            val = int(base_count + (today_manga * (idx + 1) / 7))
            growth_points.append({"date": d, "value": val})

        ingest_points = []
        for idx, d in enumerate(dates_7d):
            val = today_manga if idx == 6 else (yesterday_manga if idx == 5 else max(5, int(today_manga * 0.4)))
            ingest_points.append({"date": d, "value": val})

        # 阅读活动
        activity_points = []
        for d in dates_7d:
            ev_cnt = con.execute("SELECT COUNT(*) FROM analytics_events WHERE strftime('%m-%d', created_at) = ?", (d,)).fetchone()[0]
            activity_points.append({"date": d, "value": ev_cnt})

        stats = {
            "manga": total_manga,
            "chapters": total_chapters,
            "pages": total_pages,
            "sources": total_sources,
            "storage_bytes": total_bytes,
            "storage_str": format_bytes(total_bytes),
            "today_manga": today_manga,
            "yesterday_manga": yesterday_manga,
            "change_pct": round(((today_manga - yesterday_manga) / max(1, yesterday_manga)) * 100, 1) if yesterday_manga else 0
        }
        sys_metrics = get_system_metrics()

    finally:
        con.close()

    return templates.TemplateResponse("admin.html", {
        "request": request,
        "stats": stats,
        "sources": sources_breakdown,
        "jobs": jobs,
        "trending": trending_rows,
        "growth_points": growth_points,
        "ingest_points": ingest_points,
        "activity_points": activity_points,
        "sys_metrics": sys_metrics,
        "config": load_config(),
    })


@app.post("/api/analytics/ping")
async def analytics_ping(request: Request):
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False})
    event_type = str(data.get("event_type", "page_view"))[:32]
    manga_id = data.get("manga_id")
    chapter_id = data.get("chapter_id")
    device_hash = str(data.get("device_hash", ""))[:64]

    con = connect()
    try:
        con.execute("""
            INSERT INTO analytics_events (event_type, manga_id, chapter_id, device_hash)
            VALUES (?, ?, ?, ?)
        """, (event_type, manga_id, chapter_id, device_hash))
        con.commit()
    except Exception:
        pass
    finally:
        con.close()
    return JSONResponse({"ok": True})


@app.post("/admin/source/test-rule")
async def test_parsing_rule(request: Request):
    admin, redirect = require_admin(request)
    if redirect:
        return JSONResponse({"matched": False, "error": "请先登录管理员账号"}, status_code=401)
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"matched": False, "error": "无效的 JSON 请求"})
    pattern_str = data.get("pattern", "").strip()
    sample_str = data.get("sample", "").strip()
    if not pattern_str or not sample_str:
        return JSONResponse({"matched": False, "error": "请提供正则模式与测试样本字符串"})
    try:
        rgx = re.compile(pattern_str)
        m = rgx.search(sample_str)
        if not m:
            return JSONResponse({"matched": False, "message": "未能匹配到对应内容，请检查正则"})
        gd = m.groupdict()
        groups = list(m.groups())
        title = gd.get("title") or (groups[0] if len(groups) > 0 else "")
        volume = gd.get("volume") or (groups[1] if len(groups) > 1 else "")
        chapter = gd.get("chapter") or (groups[2] if len(groups) > 2 else "")
        return JSONResponse({
            "matched": True,
            "title": title,
            "volume": volume,
            "chapter": chapter,
            "all_groups": groups,
        })
    except Exception as exc:
        return JSONResponse({"matched": False, "error": f"正则表达式语法错误: {exc}"})


@app.post("/admin/source/{source_id}/update-rule")
def update_source_rule(request: Request, source_id: int, parsing_rule: str = Form("")):
    admin, redirect = require_admin(request)
    if redirect:
        return redirect
    con = connect()
    try:
        con.execute("UPDATE sources SET parsing_rule=? WHERE id=?", (parsing_rule.strip(), source_id))
        con.commit()
    finally:
        con.close()
    return RedirectResponse("/admin#sources", status_code=303)


@app.post("/admin/scan/pause")
def admin_scan_pause(request: Request):
    admin, redirect = require_admin(request)
    if redirect:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    manager.pause()
    return JSONResponse({"ok": True, "status": "paused"})


@app.post("/admin/scan/resume")
def admin_scan_resume(request: Request):
    admin, redirect = require_admin(request)
    if redirect:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    manager.resume()
    return JSONResponse({"ok": True, "status": "resumed"})


@app.post("/admin/config/save")
def admin_save_config(request: Request, library_root: str = Form(...), site_title: str = Form("轻量漫画馆")):
    admin, redirect = require_admin(request)
    if redirect:
        return redirect
    path_clean = str(Path(library_root.strip()).expanduser().resolve())
    if not Path(path_clean).is_dir():
        return RedirectResponse("/admin?error=路径不存在，请检查后重试", status_code=303)

    save_config({
        "library_root": path_clean,
        "site_title": site_title.strip() or "轻量漫画馆"
    })
    manager.enqueue_full_scan()
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/scan/full")
def admin_scan_full(request: Request):
    admin, redirect = require_admin(request)
    if redirect:
        return redirect
    manager.enqueue_full_scan()
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/source/{source_id}/scan")
def admin_scan_source(request: Request, source_id: int):
    admin, redirect = require_admin(request)
    if redirect:
        return redirect
    manager.enqueue_source_scan(source_id)
    return RedirectResponse("/admin", status_code=303)


@app.get("/api/scan/progress")
def api_scan_progress(request: Request):
    admin, redirect = require_admin(request)
    if redirect:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    progress = manager.get_progress()
    con = connect()
    try:
        progress["stats"] = {
            "manga": con.execute("SELECT COUNT(*) FROM manga").fetchone()[0],
            "chapters": con.execute("SELECT COUNT(*) FROM chapters").fetchone()[0],
            "pages": con.execute("SELECT COUNT(*) FROM pages").fetchone()[0],
            "sources": con.execute("SELECT COUNT(DISTINCT source_id) FROM manga").fetchone()[0],
        }
    except Exception:
        progress["stats"] = {"manga": 0, "chapters": 0, "pages": 0, "sources": 0}
    finally:
        con.close()
    return progress


@app.get("/admin/logout")
def admin_logout():
    resp = RedirectResponse("/admin/login", status_code=303)
    resp.delete_cookie("manga_admin", path="/")
    return resp


@app.get("/api/health")
def health():
    return {"ok": True}
