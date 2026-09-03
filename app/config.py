from pathlib import Path
import json
import secrets

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
CONFIG_PATH = DATA_DIR / "config.json"

DEFAULT = {
    "library_root": "/home/leaf/D/.漫画",
    "site_title": "轻量漫画库",
    "thumb_width": 320,
    "secret_key": secrets.token_urlsafe(48),
    "session_max_age": 60 * 60 * 24 * 7,
    "require_admin_login": False,
    "watch_debounce_seconds": 2.0,
}


def load_config() -> dict:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULT, ensure_ascii=False, indent=2), encoding="utf-8")
        return dict(DEFAULT)
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        merged = dict(DEFAULT)
        merged.update(data)
        return merged
    except Exception:
        return dict(DEFAULT)


def save_config(data: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    current = load_config()
    current.update(data)
    CONFIG_PATH.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
