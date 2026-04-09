"""
Split — Shared Expense Calculator
FastAPI + SQLite + JWT (httpOnly cookies) + TOTP MFA
"""
import io, os, re, secrets as _secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
import pyotp
import qrcode
import qrcode.image.svg
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.staticfiles import StaticFiles
from jose import JWTError, jwt
from pydantic import BaseModel
import sqlite3

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Config  (override via environment variables in docker-stack.yml)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _secret(name: str, default: str = "") -> str:
    """Read Docker Secret from file if <NAME>_FILE env var is set, else fall back to <NAME>."""
    file_path = os.environ.get(f"{name}_FILE")
    if file_path and os.path.isfile(file_path):
        return open(file_path).read().strip()
    return os.environ.get(name, default)

SECRET_KEY    = _secret("SECRET_KEY") or _secrets.token_hex(32)
ALGORITHM     = "HS256"
ACCESS_TTL    = int(os.environ.get("TOKEN_HOURS", "24"))   # hours
MFA_TTL       = 5                                           # minutes
DB_PATH       = os.environ.get("DB_PATH", "/data/split.db")
APP_NAME      = "Split"
SECURE_COOKIE = os.environ.get("SECURE_COOKIE", "false").lower() == "true"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Database
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _connect() -> sqlite3.Connection:
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    
    # Bump the timeout to 15 seconds to ride out any NFS latency
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=15.0)
    conn.row_factory = sqlite3.Row
    
    # CRITICAL: WAL mode breaks over NFS. Use TRUNCATE instead.
    conn.execute("PRAGMA journal_mode=TRUNCATE")
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def get_db():
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()

def init_db():
    conn = _connect()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT    UNIQUE NOT NULL COLLATE NOCASE,
            password_hash TEXT    NOT NULL,
            totp_secret   TEXT,
            mfa_enabled   INTEGER NOT NULL DEFAULT 0
        );

        -- Exactly two rows (slot 1 and 2) — one per person
        CREATE TABLE IF NOT EXISTS people (
            slot      INTEGER PRIMARY KEY,   -- 1 or 2
            name      TEXT    NOT NULL DEFAULT '',
            paycheck  REAL    NOT NULL DEFAULT 0,
            frequency INTEGER NOT NULL DEFAULT 26   -- paychecks / year
        );

        CREATE TABLE IF NOT EXISTS bills (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            name      TEXT NOT NULL,
            amount    REAL NOT NULL,
            frequency TEXT NOT NULL DEFAULT 'monthly'
            -- monthly | quarterly | yearly
        );

        INSERT OR IGNORE INTO people(slot,name,paycheck,frequency) VALUES(1,'',0,26);
        INSERT OR IGNORE INTO people(slot,name,paycheck,frequency) VALUES(2,'',0,26);
    """)

    # Seed users from environment variables (safe to re-run; INSERT OR IGNORE)
    for i in (1, 2):
        uname  = _secret(f"USER{i}_USERNAME").strip()
        passwd = _secret(f"USER{i}_PASSWORD").strip()
        if uname and passwd:
            h = bcrypt.hashpw(passwd.encode(), bcrypt.gensalt()).decode()
            conn.execute(
                "INSERT OR IGNORE INTO users(username,password_hash) VALUES(?,?)",
                (uname, h),
            )

    conn.commit()
    conn.close()

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# JWT helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def _make_token(user_id: int, token_type: str, ttl: timedelta) -> str:
    return jwt.encode(
        {"sub": str(user_id), "type": token_type,
         "exp": datetime.now(timezone.utc) + ttl},
        SECRET_KEY, algorithm=ALGORITHM,
    )

def _decode(token: str) -> dict:
    return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])

def _cookie_kwargs(max_age: int) -> dict:
    return dict(httponly=True, samesite="strict", secure=SECURE_COOKIE, max_age=max_age)

def _user_from_token(token: str | None, expected_type: str,
                     db: sqlite3.Connection) -> dict:
    if not token:
        raise HTTPException(401, "Authentication required")
    try:
        p = _decode(token)
        if p.get("type") != expected_type:
            raise ValueError("wrong type")
    except (JWTError, ValueError):
        raise HTTPException(401, "Session expired — please log in again")
    row = db.execute("SELECT * FROM users WHERE id=?", (p["sub"],)).fetchone()
    if not row:
        raise HTTPException(401, "User not found")
    return dict(row)

def require_auth(request: Request, db=Depends(get_db)) -> dict:
    return _user_from_token(request.cookies.get("access_token"), "access", db)

def require_mfa_pending(request: Request, db=Depends(get_db)) -> dict:
    return _user_from_token(request.cookies.get("mfa_token"), "mfa_pending", db)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# FastAPI
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
app = FastAPI(title=APP_NAME, docs_url=None, redoc_url=None)

@app.on_event("startup")
def on_startup():
    init_db()

# ── Pydantic request bodies ──────────────────────────────────────────
class LoginIn(BaseModel):
    username: str
    password: str

class CodeIn(BaseModel):
    code: str

class PersonIn(BaseModel):
    slot: int           # 1 or 2
    name: str
    paycheck: float
    frequency: int      # 52 | 26 | 24 | 12

class BillIn(BaseModel):
    name: str
    amount: float
    frequency: str      # monthly | quarterly | yearly

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Auth routes
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

DUMMY_HASH = bcrypt.hashpw(b"placeholder", bcrypt.gensalt())

@app.post("/api/auth/login")
def login(body: LoginIn, response: Response, db=Depends(get_db)):
    """Step 1 — validate username + password, issue short-lived MFA pending cookie."""
    user = db.execute(
        "SELECT * FROM users WHERE username=?", (body.username.strip(),)
    ).fetchone()
    
    candidate  = body.password.encode()
    valid_hash = user["password_hash"].encode() if user else DUMMY_HASH
    
    # Catch any potential bcrypt errors just in case
    try:
        ok = bcrypt.checkpw(candidate, valid_hash) and bool(user)
    except ValueError:
        ok = False

    if not ok:
        raise HTTPException(401, "Invalid username or password")

    user = dict(user)
    token = _make_token(user["id"], "mfa_pending", timedelta(minutes=MFA_TTL))
    response.set_cookie("mfa_token", token, **_cookie_kwargs(MFA_TTL * 60))
    return {"status": "mfa_setup_required" if not user["mfa_enabled"] else "mfa_required"}


@app.get("/api/auth/setup-mfa")
def setup_mfa(response: Response, user=Depends(require_mfa_pending), db=Depends(get_db)):
    """Step 2a (first-time MFA) — generate TOTP secret, return QR code SVG."""
    secret = user["totp_secret"] or pyotp.random_base32()
    db.execute("UPDATE users SET totp_secret=? WHERE id=?", (secret, user["id"]))
    db.commit()

    uri = pyotp.TOTP(secret).provisioning_uri(name=user["username"], issuer_name=APP_NAME)
    qr  = qrcode.QRCode(box_size=5, border=2)
    qr.add_data(uri)
    qr.make(fit=True)
    img = qr.make_image(image_factory=qrcode.image.svg.SvgPathImage)
    buf = io.BytesIO()
    img.save(buf)
    svg = buf.getvalue().decode()
    # Make SVG scale with its container
    svg = re.sub(r'(?<=<svg)[^>]*', lambda m: re.sub(r'width="[^"]*"', '', re.sub(r'height="[^"]*"', '', m.group())), svg, count=1)
    return {"secret": secret, "qr_svg": svg}


@app.post("/api/auth/confirm-mfa")
def confirm_mfa(body: CodeIn, response: Response,
                user=Depends(require_mfa_pending), db=Depends(get_db)):
    """Step 2b — verify TOTP code from authenticator app, enable MFA, issue session."""
    _check_totp(user, body.code)
    db.execute("UPDATE users SET mfa_enabled=1 WHERE id=?", (user["id"],))
    db.commit()
    _issue_session(response, user["id"])
    return {"status": "ok"}


@app.post("/api/auth/verify-mfa")
def verify_mfa(body: CodeIn, response: Response,
               user=Depends(require_mfa_pending), db=Depends(get_db)):
    """Step 2 (returning user) — verify TOTP, issue full session cookie."""
    _check_totp(user, body.code)
    _issue_session(response, user["id"])
    return {"status": "ok"}


@app.post("/api/auth/logout")
def logout(response: Response):
    response.delete_cookie("access_token")
    response.delete_cookie("mfa_token")
    return {"status": "ok"}


@app.get("/api/auth/me")
def me(user=Depends(require_auth)):
    return {"id": user["id"], "username": user["username"],
            "mfa_enabled": bool(user["mfa_enabled"])}


def _check_totp(user: dict, code: str):
    if not user.get("totp_secret"):
        raise HTTPException(400, "MFA not configured")
    totp = pyotp.TOTP(user["totp_secret"])
    if not totp.verify(code.replace(" ", ""), valid_window=1):
        raise HTTPException(400, "Invalid code — check your authenticator app")

def _issue_session(response: Response, user_id: int):
    token = _make_token(user_id, "access", timedelta(hours=ACCESS_TTL))
    response.delete_cookie("mfa_token")
    response.set_cookie("access_token", token, **_cookie_kwargs(ACCESS_TTL * 3600))

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# People routes  (shared data — both users see & edit the same rows)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@app.get("/api/people")
def get_people(user=Depends(require_auth), db=Depends(get_db)):
    rows = db.execute("SELECT * FROM people ORDER BY slot").fetchall()
    return [dict(r) for r in rows]


@app.put("/api/people")
def update_person(body: PersonIn, user=Depends(require_auth), db=Depends(get_db)):
    if body.slot not in (1, 2):
        raise HTTPException(400, "slot must be 1 or 2")
    if body.frequency not in (52, 26, 24, 12):
        raise HTTPException(400, "frequency must be 52, 26, 24, or 12")
    if body.paycheck < 0:
        raise HTTPException(400, "paycheck must be non-negative")
    db.execute(
        "UPDATE people SET name=?,paycheck=?,frequency=? WHERE slot=?",
        (body.name.strip(), body.paycheck, body.frequency, body.slot),
    )
    db.commit()
    return {"status": "ok"}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Bills routes
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
VALID_BILL_FREQ = {"bi-weekly", "monthly", "quarterly", "yearly"}

@app.get("/api/bills")
def get_bills(user=Depends(require_auth), db=Depends(get_db)):
    rows = db.execute("SELECT * FROM bills ORDER BY id").fetchall()
    return [dict(r) for r in rows]


@app.post("/api/bills")
def add_bill(body: BillIn, user=Depends(require_auth), db=Depends(get_db)):
    _validate_bill(body)
    cur = db.execute(
        "INSERT INTO bills(name,amount,frequency) VALUES(?,?,?)",
        (body.name.strip(), body.amount, body.frequency),
    )
    db.commit()
    row = db.execute("SELECT * FROM bills WHERE id=?", (cur.lastrowid,)).fetchone()
    return dict(row)


@app.put("/api/bills/{bill_id}")
def update_bill(bill_id: int, body: BillIn,
                user=Depends(require_auth), db=Depends(get_db)):
    _validate_bill(body)
    affected = db.execute(
        "UPDATE bills SET name=?,amount=?,frequency=? WHERE id=?",
        (body.name.strip(), body.amount, body.frequency, bill_id),
    ).rowcount
    db.commit()
    if not affected:
        raise HTTPException(404, "Bill not found")
    return {"status": "ok"}


@app.delete("/api/bills/{bill_id}")
def delete_bill(bill_id: int, user=Depends(require_auth), db=Depends(get_db)):
    db.execute("DELETE FROM bills WHERE id=?", (bill_id,))
    db.commit()
    return {"status": "ok"}


def _validate_bill(body: BillIn):
    if body.frequency not in VALID_BILL_FREQ:
        raise HTTPException(400, f"frequency must be one of {VALID_BILL_FREQ}")
    if body.amount < 0:
        raise HTTPException(400, "amount must be non-negative")
    if not body.name.strip():
        raise HTTPException(400, "name is required")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Calculation route
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_FREQ_MULTIPLIER = {
    "bi-weekly": 26 / 12,
    "monthly": 1.0, 
    "quarterly": 1 / 3, 
    "yearly": 1 / 12
}

@app.get("/api/calculate")
def calculate(user=Depends(require_auth), db=Depends(get_db)):
    people = [dict(r) for r in db.execute("SELECT * FROM people ORDER BY slot").fetchall()]
    bills  = [dict(r) for r in db.execute("SELECT * FROM bills ORDER BY id").fetchall()]

    p1, p2 = people[0], people[1]
    annual1 = p1["paycheck"] * p1["frequency"]
    annual2 = p2["paycheck"] * p2["frequency"]
    total   = annual1 + annual2

    pct1 = (annual1 / total) if total > 0 else 0.5
    pct2 = (annual2 / total) if total > 0 else 0.5

    bill_rows, total_monthly = [], 0.0
    for b in bills:
        mo = b["amount"] * _FREQ_MULTIPLIER.get(b["frequency"], 1.0)
        total_monthly += mo
        bill_rows.append({
            **b,
            "monthly_amount": mo,
            "share1": mo * pct1,
            "share2": mo * pct2,
        })

    c1_mo  = total_monthly * pct1
    c2_mo  = total_monthly * pct2
    # Per-paycheck = monthly × 12 / paychecks_per_year
    c1_pc  = (c1_mo * 12 / p1["frequency"]) if p1["frequency"] else 0
    c2_pc  = (c2_mo * 12 / p2["frequency"]) if p2["frequency"] else 0

    return {
        "people": [
            {**p1, "annual": annual1, "monthly_income": annual1 / 12,
             "pct": pct1, "contrib_monthly": c1_mo, "contrib_paycheck": c1_pc},
            {**p2, "annual": annual2, "monthly_income": annual2 / 12,
             "pct": pct2, "contrib_monthly": c2_mo, "contrib_paycheck": c2_pc},
        ],
        "bills": bill_rows,
        "total_monthly": total_monthly,
        "total_annual":  total_monthly * 12,
    }

# ── Serve the single-page frontend ──────────────────────────────────
# Must be LAST — catches everything not matched above
app.mount("/", StaticFiles(directory="static", html=True), name="static")
