from __future__ import annotations

import math
import secrets
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

BASE_DIR = Path(__file__).resolve().parent.parent
THUMB_DIR = BASE_DIR / "data" / "thumbnails"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".avif", ".gif"}

templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))
app = FastAPI(title="轻量漫画库", docs_url=None, redoc_url=None)
app.add_middleware(GZipMiddleware, minimum_size=1024)
manager = ScanManager()
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "app" / "static")), name="static")


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
        manga_path = site_path / row["manga_rel"]
        img_path = (manga_path / row["cover_rel_path"]).resolve()

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
    manga_path = site_path / row["manga_rel"]
    file_path = (manga_path / row["page_rel"]).resolve()

    try:
        file_path.relative_to(root)
    except ValueError:
        return JSONResponse({"error": "access denied"}, status_code=403)

    if not file_path.is_file() or file_path.suffix.lower() not in IMAGE_EXTS:
        return JSONResponse({"error": "file not found"}, status_code=404)

    return FileResponse(file_path, headers={"Cache-Control": "public, max-age=604800"})


@app.get("/admin", response_class=HTMLResponse)
def admin_home(request: Request):
    con = connect()
    try:
        stats = {
            "manga": con.execute("SELECT COUNT(*) FROM manga").fetchone()[0],
            "chapters": con.execute("SELECT COUNT(*) FROM chapters").fetchone()[0],
            "pages": con.execute("SELECT COUNT(*) FROM pages").fetchone()[0],
            "sources": con.execute("SELECT COUNT(DISTINCT source_id) FROM manga").fetchone()[0],
        }
        sources = con.execute("""
            SELECT s.*, COUNT(m.id) as manga_count
            FROM sources s
            LEFT JOIN manga m ON m.source_id = s.id
            GROUP BY s.id
            ORDER BY s.name ASC
        """).fetchall()
        jobs = con.execute("SELECT * FROM scan_jobs ORDER BY id DESC LIMIT 10").fetchall()
    finally:
        con.close()

    return templates.TemplateResponse("admin.html", {
        "request": request,
        "stats": stats,
        "sources": sources,
        "jobs": jobs,
        "config": load_config(),
    })


@app.post("/admin/config/save")
def admin_save_config(request: Request, library_root: str = Form(...), site_title: str = Form("轻量漫画馆")):
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
def admin_scan_full():
    manager.enqueue_full_scan()
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/source/{source_id}/scan")
def admin_scan_source(source_id: int):
    manager.enqueue_source_scan(source_id)
    return RedirectResponse("/admin", status_code=303)


@app.get("/api/scan/progress")
def api_scan_progress():
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
    return RedirectResponse("/", status_code=303)


@app.get("/api/health")
def health():
    return {"ok": True}
