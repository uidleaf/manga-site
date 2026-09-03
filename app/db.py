from pathlib import Path
import sqlite3

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "manga.db"

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    rel_path TEXT NOT NULL UNIQUE,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS manga (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    rel_path TEXT NOT NULL,
    cover_rel_path TEXT,
    chapter_count INTEGER NOT NULL DEFAULT 1,
    page_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source_id, rel_path)
);
CREATE INDEX IF NOT EXISTS idx_manga_source ON manga(source_id);
CREATE INDEX IF NOT EXISTS idx_manga_title ON manga(title);

CREATE TABLE IF NOT EXISTS chapters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    manga_id INTEGER NOT NULL REFERENCES manga(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    volume TEXT,
    chapter_num TEXT,
    rel_path TEXT NOT NULL,
    page_count INTEGER NOT NULL DEFAULT 0,
    order_num INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(manga_id, rel_path)
);
CREATE INDEX IF NOT EXISTS idx_chapters_manga ON chapters(manga_id, order_num);

CREATE TABLE IF NOT EXISTS pages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chapter_id INTEGER NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
    manga_id INTEGER NOT NULL REFERENCES manga(id) ON DELETE CASCADE,
    page_number INTEGER NOT NULL,
    rel_path TEXT NOT NULL,
    width INTEGER,
    height INTEGER,
    file_size INTEGER,
    mtime_ns INTEGER,
    format TEXT,
    UNIQUE(chapter_id, page_number)
);
CREATE INDEX IF NOT EXISTS idx_pages_chapter ON pages(chapter_id, page_number);
CREATE INDEX IF NOT EXISTS idx_pages_manga ON pages(manga_id);

CREATE TABLE IF NOT EXISTS admin_users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS scan_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER REFERENCES sources(id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    message TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at TEXT,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS analytics_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    manga_id INTEGER,
    chapter_id INTEGER,
    device_hash TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_analytics_type_date ON analytics_events(event_type, created_at);
CREATE INDEX IF NOT EXISTS idx_analytics_manga ON analytics_events(manga_id);

CREATE TABLE IF NOT EXISTS analytics_daily (
    date TEXT PRIMARY KEY,
    manga_count INTEGER DEFAULT 0,
    chapter_count INTEGER DEFAULT 0,
    image_count INTEGER DEFAULT 0,
    storage_bytes INTEGER DEFAULT 0,
    reading_sessions INTEGER DEFAULT 0,
    active_devices INTEGER DEFAULT 0
);
"""


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=60)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=10000")
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = connect()
    try:
        con.executescript(SCHEMA)
        cols = [r["name"] for r in con.execute("PRAGMA table_info(sources)").fetchall()]
        if "parsing_rule" not in cols:
            con.execute("ALTER TABLE sources ADD COLUMN parsing_rule TEXT")
        con.commit()
    finally:
        con.close()
