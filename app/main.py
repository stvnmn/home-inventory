import asyncio
import base64
import hashlib
import hmac
import os
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager, closing
from datetime import date, timedelta
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

DB_PATH = os.getenv("DB_PATH", "/data/inventory.db")
# Env values are only defaults; anything saved from the Settings page takes precedence.
SETTING_DEFAULTS = {
    "pushover_token": os.getenv("PUSHOVER_APP_TOKEN", ""),
    "pushover_user": os.getenv("PUSHOVER_USER_KEY", ""),
    "restock_lead_days": os.getenv("RESTOCK_LEAD_DAYS", "14"),
    "expiry_warn_days": os.getenv("EXPIRY_WARN_DAYS", "30"),
}
SESSION_DAYS = 30
CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "3600"))
STATIC_DIR = os.path.join(os.path.dirname(__file__), "..", "static")

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
  id INTEGER PRIMARY KEY,
  barcode TEXT UNIQUE,
  name TEXT NOT NULL,
  category TEXT
);
CREATE TABLE IF NOT EXISTS units (
  id INTEGER PRIMARY KEY,
  product_id INTEGER NOT NULL REFERENCES products(id),
  added_on TEXT NOT NULL,
  expires_on TEXT,
  finished_on TEXT
);
CREATE TABLE IF NOT EXISTS notified (key TEXT PRIMARY KEY, sent_on TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS users (username TEXT PRIMARY KEY COLLATE NOCASE, password_hash TEXT NOT NULL);
"""


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with closing(db()) as c:
        c.executescript(SCHEMA)
        # Carry over the single account created before multi-user support existed.
        old = {r["key"]: r["value"] for r in c.execute("SELECT key, value FROM settings WHERE key IN ('auth_user','auth_hash')")}
        if old.get("auth_user") and old.get("auth_hash"):
            c.execute("INSERT OR IGNORE INTO users (username, password_hash) VALUES (?,?)", (old["auth_user"], old["auth_hash"]))
            c.execute("DELETE FROM settings WHERE key IN ('auth_user','auth_hash')")
        c.commit()


def get_setting(key: str, default: Optional[str] = None) -> str:
    with closing(db()) as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row:
        return row["value"]
    return SETTING_DEFAULTS.get(key, "") if default is None else default


def set_setting(key: str, value: str):
    with closing(db()) as c:
        c.execute("INSERT INTO settings (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        c.commit()


def int_setting(key: str) -> int:
    fallback = {"restock_lead_days": 14, "expiry_warn_days": 30}[key]
    try:
        return int(get_setting(key))
    except ValueError:
        return fallback


# ---- auth (single local user, salted PBKDF2 hash, signed session cookie) ----
def hash_password(password: str, salt: Optional[bytes] = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return salt.hex() + "$" + digest.hex()


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), 200_000)
    return hmac.compare_digest(digest.hex(), digest_hex)


def secret_key() -> bytes:
    key = get_setting("session_secret", "")
    if not key:
        key = secrets.token_hex(32)
        set_setting("session_secret", key)
    return key.encode()


def get_user(username: str):
    with closing(db()) as c:
        return c.execute("SELECT username, password_hash FROM users WHERE username=?", (username,)).fetchone()


def make_session(username: str) -> str:
    # Tying the token to the password hash invalidates old sessions when the password changes.
    fp = get_user(username)["password_hash"][-12:]
    payload = f"{username}|{int(time.time()) + SESSION_DAYS * 86400}|{fp}"
    sig = hmac.new(secret_key(), payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(payload.encode()).decode() + "." + sig


def valid_session(token: Optional[str]) -> Optional[str]:
    """Returns the username for a valid session token, else None."""
    if not token or "." not in token:
        return None
    try:
        b64, sig = token.rsplit(".", 1)
        payload = base64.urlsafe_b64decode(b64.encode()).decode()
        name, exp, fp = payload.split("|")
        exp = int(exp)
    except Exception:
        return None
    good = hmac.new(secret_key(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, good) or exp <= time.time():
        return None
    user = get_user(name)
    if user and user["password_hash"][-12:] == fp:
        return user["username"]
    return None


def user_exists() -> bool:
    with closing(db()) as c:
        return c.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None


failures: dict = {}  # client ip -> (count, locked_until)


def check_throttle(ip: str):
    count, until = failures.get(ip, (0, 0))
    if until > time.time():
        raise HTTPException(429, f"Too many attempts. Try again in {int(until - time.time())}s")


def record_failure(ip: str):
    count = failures.get(ip, (0, 0))[0] + 1
    failures[ip] = (0, time.time() + 60) if count >= 5 else (count, 0)


def product_stats(c, product_id: int, today: date, lead_days: int = 14) -> dict:
    rows = c.execute(
        "SELECT added_on, expires_on, finished_on FROM units WHERE product_id=? ORDER BY added_on, id",
        (product_id,),
    ).fetchall()
    finished = sorted(
        (date.fromisoformat(r["finished_on"]), date.fromisoformat(r["added_on"]))
        for r in rows
        if r["finished_on"]
    )
    # A unit's use period starts when it was added or when the previous one ran out, whichever is later.
    durations, prev_finish = [], None
    for fin, added in finished:
        start = max(added, prev_finish) if prev_finish else added
        durations.append(max((fin - start).days, 1))
        prev_finish = fin
    avg = sum(durations) / len(durations) if durations else None

    in_stock = [r for r in rows if not r["finished_on"]]
    runout = restock_on = None
    if avg and in_stock:
        first_added = date.fromisoformat(in_stock[0]["added_on"])
        start = max(first_added, prev_finish) if prev_finish else first_added
        runout = start + timedelta(days=round(avg * len(in_stock)))
        restock_on = runout - timedelta(days=lead_days)
    expiries = sorted(r["expires_on"] for r in in_stock if r["expires_on"])
    return {
        "in_stock": len(in_stock),
        "avg_days_per_unit": round(avg, 1) if avg else None,
        "units_finished": len(finished),
        "predicted_runout": runout.isoformat() if runout else None,
        "restock_on": restock_on.isoformat() if restock_on else None,
        "needs_restock": bool(
            (restock_on and today >= restock_on) or (avg and not in_stock)
        ),
        "next_expiry": expiries[0] if expiries else None,
    }


def list_products(c, today: date):
    out = []
    lead = int_setting("restock_lead_days")
    for p in c.execute("SELECT * FROM products ORDER BY name").fetchall():
        out.append({**dict(p), **product_stats(c, p["id"], today, lead)})
    return out


class ProductIn(BaseModel):
    name: str
    barcode: Optional[str] = None
    category: Optional[str] = None


class StockIn(BaseModel):
    product_id: int
    quantity: int = 1
    expires_on: Optional[date] = None


class FinishIn(BaseModel):
    product_id: int
    finished_on: Optional[date] = None


async def lookup_barcode(code: str) -> Optional[str]:
    try:
        async with httpx.AsyncClient(timeout=6) as client:
            r = await client.get(f"https://world.openfoodfacts.org/api/v2/product/{code}.json")
        if r.status_code == 200 and r.json().get("status") == 1:
            p = r.json()["product"]
            name = p.get("product_name") or ""
            brand = (p.get("brands") or "").split(",")[0]
            return f"{brand} {name}".strip() or None
    except Exception:
        pass
    return None


async def push_detailed(title: str, message: str):
    token, user = get_setting("pushover_token"), get_setting("pushover_user")
    if not (token and user):
        print(f"[notify-disabled] {title}: {message}")
        return False, "Pushover is not configured"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                "https://api.pushover.net/1/messages.json",
                data={"token": token, "user": user, "title": title, "message": message},
            )
    except httpx.HTTPError as e:
        return False, f"Could not reach Pushover: {e.__class__.__name__}"
    if r.status_code == 200:
        return True, "Sent"
    try:
        errors = ", ".join(r.json().get("errors", []))
    except ValueError:
        errors = ""
    return False, errors or f"Pushover returned {r.status_code}"


async def push(title: str, message: str) -> bool:
    return (await push_detailed(title, message))[0]


async def run_checks() -> int:
    today = date.today()
    alerts = []
    with closing(db()) as c:
        for p in list_products(c, today):
            if p["needs_restock"]:
                key = f"restock:{p['id']}:{p['predicted_runout']}:{p['in_stock']}"
                when = f"predicted run-out {p['predicted_runout']}" if p["predicted_runout"] else "out of stock"
                alerts.append((key, "Restock soon", f"{p['name']}: {when}"))
        soon = (today + timedelta(days=int_setting("expiry_warn_days"))).isoformat()
        for u in c.execute(
            "SELECT u.id, u.expires_on, p.name FROM units u JOIN products p ON p.id=u.product_id "
            "WHERE u.finished_on IS NULL AND u.expires_on IS NOT NULL AND u.expires_on <= ?",
            (soon,),
        ):
            state = "EXPIRED" if u["expires_on"] < today.isoformat() else "expires"
            alerts.append((f"expiry:{u['id']}", "Expiring", f"{u['name']} {state} {u['expires_on']}"))
        sent = 0
        for key, title, msg in alerts:
            if c.execute("SELECT 1 FROM notified WHERE key=?", (key,)).fetchone():
                continue
            if await push(title, msg):
                c.execute("INSERT INTO notified VALUES (?,?)", (key, today.isoformat()))
                c.commit()
                sent += 1
    return sent


async def checker_loop():
    while True:
        try:
            await run_checks()
        except Exception as e:
            print("check failed:", e)
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(app):
    init_db()
    task = asyncio.create_task(checker_loop())
    yield
    task.cancel()


app = FastAPI(title="Home Inventory", lifespan=lifespan)

PUBLIC_PATHS = {"/login", "/api/auth/status", "/api/auth/login", "/api/auth/signup"}


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    if path in PUBLIC_PATHS or path == "/static/login.html" or valid_session(request.cookies.get("session")):
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse({"detail": "Not logged in"}, status_code=401)
    return RedirectResponse("/login")


class Credentials(BaseModel):
    username: str = ""
    password: str


class PasswordChange(BaseModel):
    current_password: str
    new_password: str


def set_session_cookie(request: Request, response: JSONResponse, username: str):
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    response.set_cookie(
        "session", make_session(username), max_age=SESSION_DAYS * 86400,
        httponly=True, samesite="lax", secure=secure,
    )


@app.get("/login")
def login_page():
    return FileResponse(os.path.join(STATIC_DIR, "login.html"))


@app.get("/api/auth/status")
def auth_status(request: Request):
    return {"setup_needed": not user_exists(), "logged_in": valid_session(request.cookies.get("session")) is not None}


@app.post("/api/auth/signup")
def auth_signup(body: Credentials, request: Request):
    ip = request.client.host if request.client else "?"
    check_throttle(ip)
    username = body.username.strip()
    if not 1 <= len(username) <= 40 or len(body.password) < 8:
        raise HTTPException(400, "Username required (max 40 chars) and password must be at least 8 characters")
    with closing(db()) as c:
        try:
            c.execute("INSERT INTO users (username, password_hash) VALUES (?,?)", (username, hash_password(body.password)))
        except sqlite3.IntegrityError:
            raise HTTPException(409, "That username is taken")
        c.commit()
    resp = JSONResponse({"ok": True})
    set_session_cookie(request, resp, username)
    return resp


DUMMY_HASH = hash_password("dummy-password")


@app.post("/api/auth/login")
def auth_login(body: Credentials, request: Request):
    ip = request.client.host if request.client else "?"
    check_throttle(ip)
    user = get_user(body.username.strip())
    # Hash against a dummy when the user is unknown so response time doesn't reveal valid usernames.
    ok = verify_password(body.password, user["password_hash"] if user else DUMMY_HASH) and user is not None
    if not ok:
        record_failure(ip)
        raise HTTPException(401, "Wrong username or password")
    failures.pop(ip, None)
    resp = JSONResponse({"ok": True})
    set_session_cookie(request, resp, user["username"])
    return resp


@app.post("/api/auth/logout")
def auth_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("session")
    return resp


@app.post("/api/auth/password")
def auth_password(body: PasswordChange, request: Request):
    name = valid_session(request.cookies.get("session"))
    user = get_user(name) if name else None
    if not user or not verify_password(body.current_password, user["password_hash"]):
        raise HTTPException(400, "Current password is wrong")
    if len(body.new_password) < 8:
        raise HTTPException(400, "New password must be at least 8 characters")
    with closing(db()) as c:
        c.execute("UPDATE users SET password_hash=? WHERE username=?", (hash_password(body.new_password), user["username"]))
        c.commit()
    resp = JSONResponse({"ok": True})
    set_session_cookie(request, resp, user["username"])
    return resp


@app.get("/api/products")
def api_products():
    with closing(db()) as c:
        return list_products(c, date.today())


@app.get("/api/barcode/{code}")
async def api_barcode(code: str):
    with closing(db()) as c:
        p = c.execute("SELECT * FROM products WHERE barcode=?", (code,)).fetchone()
    if p:
        return {"known": True, "product": dict(p)}
    return {"known": False, "suggested_name": await lookup_barcode(code)}


@app.post("/api/products")
def api_add_product(body: ProductIn):
    if not body.name.strip():
        raise HTTPException(400, "name required")
    with closing(db()) as c:
        try:
            cur = c.execute(
                "INSERT INTO products (barcode, name, category) VALUES (?,?,?)",
                (body.barcode or None, body.name.strip(), body.category),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(409, "barcode already exists")
        c.commit()
        return {"id": cur.lastrowid}


@app.post("/api/stock")
def api_stock_in(body: StockIn):
    if not 1 <= body.quantity <= 100:
        raise HTTPException(400, "quantity must be 1-100")
    with closing(db()) as c:
        if not c.execute("SELECT 1 FROM products WHERE id=?", (body.product_id,)).fetchone():
            raise HTTPException(404, "product not found")
        for _ in range(body.quantity):
            c.execute(
                "INSERT INTO units (product_id, added_on, expires_on) VALUES (?,?,?)",
                (body.product_id, date.today().isoformat(), body.expires_on.isoformat() if body.expires_on else None),
            )
        c.commit()
    return {"ok": True}


@app.post("/api/finish")
def api_finish(body: FinishIn):
    """Marks one unit finished, using the soonest-expiring in-stock unit first."""
    with closing(db()) as c:
        u = c.execute(
            "SELECT id FROM units WHERE product_id=? AND finished_on IS NULL "
            "ORDER BY expires_on IS NULL, expires_on, added_on, id LIMIT 1",
            (body.product_id,),
        ).fetchone()
        if not u:
            raise HTTPException(404, "no units in stock")
        c.execute(
            "UPDATE units SET finished_on=? WHERE id=?",
            ((body.finished_on or date.today()).isoformat(), u["id"]),
        )
        c.commit()
    return {"ok": True}


@app.post("/api/check")
async def api_check():
    return {"notifications_sent": await run_checks()}


@app.get("/api/config")
def api_config(request: Request):
    return {
        "restock_lead_days": int_setting("restock_lead_days"),
        "expiry_warn_days": int_setting("expiry_warn_days"),
        "pushover_configured": bool(get_setting("pushover_token") and get_setting("pushover_user")),
        "pushover_user": get_setting("pushover_user"),
        "username": valid_session(request.cookies.get("session")),
    }


class SettingsIn(BaseModel):
    pushover_token: Optional[str] = None  # blank/omitted keeps the stored token
    pushover_user: Optional[str] = None
    restock_lead_days: Optional[int] = None
    expiry_warn_days: Optional[int] = None


@app.put("/api/settings")
def api_save_settings(body: SettingsIn):
    for field in ("restock_lead_days", "expiry_warn_days"):
        v = getattr(body, field)
        if v is not None:
            if not 1 <= v <= 365:
                raise HTTPException(400, f"{field} must be 1-365")
            set_setting(field, str(v))
    if body.pushover_user is not None:
        set_setting("pushover_user", body.pushover_user.strip())
    if body.pushover_token:
        set_setting("pushover_token", body.pushover_token.strip())
    return {"ok": True}


@app.post("/api/settings/test-push")
async def api_test_push():
    ok, msg = await push_detailed("Home Inventory", "Test notification - Pushover is working.")
    return {"ok": ok, "message": msg}


class ProductPatch(BaseModel):
    name: Optional[str] = None
    category: Optional[str] = None


@app.patch("/api/products/{product_id}")
def api_edit_product(product_id: int, body: ProductPatch):
    with closing(db()) as c:
        p = c.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
        if not p:
            raise HTTPException(404, "product not found")
        name = body.name.strip() if body.name is not None else p["name"]
        if not name:
            raise HTTPException(400, "name required")
        category = body.category.strip() or None if body.category is not None else p["category"]
        c.execute("UPDATE products SET name=?, category=? WHERE id=?", (name, category, product_id))
        c.commit()
    return {"ok": True}


@app.delete("/api/products/{product_id}")
def api_delete_product(product_id: int):
    with closing(db()) as c:
        c.execute("DELETE FROM units WHERE product_id=?", (product_id,))
        c.execute("DELETE FROM products WHERE id=?", (product_id,))
        c.commit()
    return {"ok": True}


@app.get("/api/history")
def api_history():
    with closing(db()) as c:
        rows = c.execute(
            "SELECT u.id, p.name, u.added_on, u.expires_on, u.finished_on FROM units u "
            "JOIN products p ON p.id=u.product_id ORDER BY u.id DESC LIMIT 200"
        ).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/units/{unit_id}/unfinish")
def api_unfinish(unit_id: int):
    with closing(db()) as c:
        c.execute("UPDATE units SET finished_on=NULL WHERE id=?", (unit_id,))
        c.commit()
    return {"ok": True}


@app.delete("/api/units/{unit_id}")
def api_delete_unit(unit_id: int):
    with closing(db()) as c:
        c.execute("DELETE FROM units WHERE id=?", (unit_id,))
        c.commit()
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
