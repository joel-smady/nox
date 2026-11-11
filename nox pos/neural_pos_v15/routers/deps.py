"""
Neural POS — Shared dependencies
- Token verification with revocation check
- Breaks circular imports: auth.py and access_log.py both need
  get_current_user without cross-dependency.
"""
import os, jwt
from typing import Optional
from fastapi import Request

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

SECRET_KEY = os.getenv("JWT_SECRET", "CHANGE-ME-set-JWT_SECRET-in-.env-file")
ALGORITHM  = "HS256"


def verify_token(token: str) -> Optional[dict]:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except Exception:
        return None

    # Check revocation list
    try:
        from models.database import get_db
        db = get_db()
        row = db.execute(
            "SELECT id FROM revoked_tokens WHERE token=?", (token,)
        ).fetchone()
        db.close()
        if row:
            return None   # token was explicitly revoked at logout
    except Exception:
        pass  # revoked_tokens table may not exist on very old DBs — allow through

    return payload


def get_current_user(request: Request) -> Optional[dict]:
    """Read JWT from cookie → Authorization header → query param."""
    token = (
        request.cookies.get("neural_token") or
        (request.headers.get("Authorization", "")[7:]
         if request.headers.get("Authorization", "").startswith("Bearer ") else None) or
        request.query_params.get("token")
    )
    if not token:
        return None
    return verify_token(token)
