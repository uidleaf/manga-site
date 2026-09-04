import time
from collections import defaultdict, deque
from functools import wraps
from typing import Callable

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from starlette.requests import Request
from starlette.responses import RedirectResponse

from .config import load_config
from .db import connect

_ph = PasswordHasher()
_attempts: dict[str, deque[float]] = defaultdict(deque)
MAX_ATTEMPTS = 6
WINDOW = 300


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _ph.verify(password_hash, password)
    except VerifyMismatchError:
        return False


def _serializer():
    return URLSafeTimedSerializer(load_config()["secret_key"], salt="manga-session-v1")


def make_session(user_id: int, username: str) -> str:
    return _serializer().dumps({"uid": user_id, "username": username})


def read_session(token: str | None) -> dict | None:
    if not token:
        return None
    try:
        return _serializer().loads(token, max_age=load_config()["session_max_age"])
    except (BadSignature, SignatureExpired):
        return None


def is_rate_limited(ip: str) -> bool:
    now = time.time()
    q = _attempts[ip]
    while q and now - q[0] > WINDOW:
        q.popleft()
    return len(q) >= MAX_ATTEMPTS


def record_failed_attempt(ip: str) -> None:
    _attempts[ip].append(time.time())


def clear_attempts(ip: str) -> None:
    _attempts.pop(ip, None)


def current_admin(request: Request) -> dict | None:
    return read_session(request.cookies.get("manga_admin"))


def require_admin(request: Request):
    admin = current_admin(request)
    if not admin:
        return None, RedirectResponse("/admin/login", status_code=303)
    return admin, None


def has_any_admin() -> bool:
    for attempt in range(5):
        try:
            con = connect()
            try:
                cnt = con.execute("SELECT COUNT(*) FROM admin_users").fetchone()[0]
                return cnt > 0
            finally:
                con.close()
        except Exception:
            if attempt < 4:
                time.sleep(0.15)
                continue
            return False
    return False


def create_admin(username: str, password: str) -> int:
    pw_hash = hash_password(password)
    for attempt in range(6):
        try:
            con = connect()
            try:
                cur = con.execute("INSERT INTO admin_users (username, password_hash) VALUES (?, ?)", (username.strip(), pw_hash))
                con.commit()
                return int(cur.lastrowid)
            finally:
                con.close()
        except Exception as e:
            if "locked" in str(e).lower() and attempt < 5:
                time.sleep(0.2 * (attempt + 1))
                continue
            raise


def authenticate(username: str, password: str) -> dict | None:
    for attempt in range(5):
        try:
            con = connect()
            try:
                row = con.execute("SELECT id, username, password_hash FROM admin_users WHERE username=?", (username.strip(),)).fetchone()
                if row and verify_password(row["password_hash"], password):
                    return {"id": row["id"], "username": row["username"]}
                return None
            finally:
                con.close()
        except Exception as e:
            if "locked" in str(e).lower() and attempt < 4:
                time.sleep(0.15)
                continue
            return None
    return None


def verify_login(username: str, password: str) -> bool:
    return authenticate(username, password) is not None
