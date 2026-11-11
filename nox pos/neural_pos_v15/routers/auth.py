"""
Neural POS — Authentication (production hardened v9)
- bcrypt password hashing (SHA-256 legacy auto-upgrade)
- JWT secret from .env
- Rate limiting: 10 attempts / 60s per IP
- Access log on login / logout / failure
- get_current_user lives in deps.py to avoid circular imports
"""
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel
from typing import Optional
import jwt, os, datetime, time
from collections import defaultdict

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

SECRET_KEY         = os.getenv("JWT_SECRET", "CHANGE-ME-set-JWT_SECRET-in-.env-file")
ALGORITHM          = "HS256"
TOKEN_EXPIRE_HOURS = int(os.getenv("JWT_EXPIRE_HOURS", "12"))
IDLE_TIMEOUT_MINS  = 60

# ── Import shared dependency (no circular risk) ───────────────────────────────
from routers.deps import get_current_user, verify_token  # noqa: re-exported

# ── bcrypt — direct (no passlib, compatible with bcrypt 4.x + Python 3.14) ───
try:
    import bcrypt as _bcrypt

    def hash_password(pw: str) -> str:
        return _bcrypt.hashpw(pw.encode("utf-8"), _bcrypt.gensalt()).decode("utf-8")

    def verify_password(pw: str, hashed: str) -> bool:
        if len(hashed) == 64 and all(c in "0123456789abcdef" for c in hashed):
            import hashlib
            return hashlib.sha256(pw.encode()).hexdigest() == hashed
        try:
            return _bcrypt.checkpw(pw.encode("utf-8"), hashed.encode("utf-8"))
        except Exception:
            return False

    BCRYPT_AVAILABLE = True

except ImportError:
    import hashlib

    def hash_password(pw: str) -> str:
        return hashlib.sha256(pw.encode()).hexdigest()

    def verify_password(pw: str, hashed: str) -> bool:
        return hash_password(pw) == hashed

    BCRYPT_AVAILABLE = False

from models.database import get_db

router = APIRouter()

# ── In-memory rate limiter ────────────────────────────────────────────────────
_login_attempts: dict = defaultdict(list)
MAX_ATTEMPTS   = 10
WINDOW_SECONDS = 60

def _check_rate_limit(ip: str):
    now      = time.time()
    attempts = [t for t in _login_attempts[ip] if now - t < WINDOW_SECONDS]
    _login_attempts[ip] = attempts
    if len(attempts) >= MAX_ATTEMPTS:
        raise HTTPException(429, f"Too many login attempts. Try again in {WINDOW_SECONDS} seconds.")
    _login_attempts[ip].append(now)

def _get_ip(request: Request) -> str:
    return (request.headers.get("X-Forwarded-For") or
            request.headers.get("X-Real-IP") or
            getattr(request.client, "host", "unknown"))

# ── Models ────────────────────────────────────────────────────────────────────
class LoginRequest(BaseModel):
    username: str
    password: str

# ── Token creation ────────────────────────────────────────────────────────────
def create_token(data: dict) -> str:
    payload      = data.copy()
    payload["exp"] = datetime.datetime.utcnow() + datetime.timedelta(hours=TOKEN_EXPIRE_HOURS)
    payload["iat"] = datetime.datetime.utcnow()
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)

def require_role(request: Request, roles: list):
    user = get_current_user(request)
    if not user:
        raise HTTPException(401, "Authentication required")
    if user.get("role") not in roles:
        raise HTTPException(403, "Insufficient permissions")
    return user

# ── Routes ────────────────────────────────────────────────────────────────────
@router.post("/login")
async def login(credentials: LoginRequest, request: Request, response: Response):
    ip = _get_ip(request)
    _check_rate_limit(ip)

    db  = get_db()
    row = db.execute(
        "SELECT id, username, full_name, role, email, password_hash, is_active "
        "FROM users WHERE username=? AND is_active=1",
        (credentials.username.strip(),)
    ).fetchone()

    if row and verify_password(credentials.password, row["password_hash"]):
        # Lazy-upgrade SHA-256 → bcrypt
        if BCRYPT_AVAILABLE and len(row["password_hash"]) == 64:
            db.execute("UPDATE users SET password_hash=? WHERE id=?",
                       (hash_password(credentials.password), row["id"]))
        db.execute("UPDATE users SET last_login=? WHERE id=?",
                   (datetime.datetime.now().isoformat(), row["id"]))
        db.commit()
        db.close()

        token_data = {
            "user_id":   row["id"],
            "username":  row["username"],
            "full_name": row["full_name"],
            "role":      row["role"],
            "email":     row["email"],
        }
        token = create_token(token_data)
        response.set_cookie(
            key="neural_token", value=token,
            httponly=False, max_age=TOKEN_EXPIRE_HOURS * 3600,
            samesite="lax", secure=False, path="/"
        )

        # Access log — deferred import avoids circular reference at module load
        try:
            from routers.access_log import log_action
            log_action(request, "login",
                       f"Successful login from {ip} · role: {row['role']}",
                       user=token_data)
        except Exception:
            pass

        return {
            "access_token": token, "token_type": "bearer",
            "user": token_data, "idle_timeout_mins": IDLE_TIMEOUT_MINS
        }

    else:
        db.close()
        try:
            from routers.access_log import log_action
            log_action(request, "login_failed",
                       f"Failed login for '{credentials.username}' from {ip}",
                       status="failed",
                       user={"username": credentials.username, "full_name": "", "role": ""})
        except Exception:
            pass
        raise HTTPException(401, "Invalid username or password")


@router.post("/logout")
async def logout(request: Request, response: Response):
    user = get_current_user(request)
    # Blacklist the token so it cannot be reused after logout
    token = (
        request.cookies.get("neural_token") or
        (request.headers.get("Authorization", "")[7:]
         if request.headers.get("Authorization", "").startswith("Bearer ") else None)
    )
    if token:
        try:
            from models.database import get_db
            db = get_db()
            db.execute(
                "INSERT OR IGNORE INTO revoked_tokens (token, revoked_at) VALUES (?, datetime('now'))",
                (token,)
            )
            db.commit()
            db.close()
        except Exception:
            pass
    response.delete_cookie("neural_token")
    try:
        from routers.access_log import log_action
        log_action(request, "logout", "User logged out", user=user or {})
    except Exception:
        pass
    return {"message": "Logged out"}


@router.get("/me")
async def get_me(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(401, "Not authenticated")
    return user


@router.get("/verify")
async def verify_auth(request: Request):
    user = get_current_user(request)
    return {"authenticated": bool(user), "user": user}
