"""
Neural POS — Access Log
Tracks every login, logout, page access, and sensitive action.
Admin can view + clear. Manager can view only. Cashier: no access.
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from typing import Optional
from models.database import get_db
from routers.deps import get_current_user
import datetime

router = APIRouter()

# ── Table ─────────────────────────────────────────────────────────────────────

# ── Public helper — called from other routers ─────────────────────────────────
def log_action(request: Request, action: str, detail: str = "",
               status: str = "success", user: dict = None):
    """Fire-and-forget access log writer. Safe to call from any router."""
    try:
        if user is None:
            user = get_current_user(request) or {}
        ip = (request.headers.get("X-Forwarded-For") or
              request.headers.get("X-Real-IP") or
              getattr(request.client, "host", "unknown"))
        ua = request.headers.get("User-Agent", "")[:200]
        db = get_db()
        db.execute("""
            INSERT INTO access_log
            (user_id, username, full_name, role, action, detail, ip_address, user_agent, status)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (
            user.get("user_id") or user.get("id"),
            user.get("username", "unknown"),
            user.get("full_name", ""),
            user.get("role", ""),
            action, detail, ip, ua, status
        ))
        db.commit(); db.close()
    except Exception:
        pass  # Never crash a request because of logging

# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("")
async def get_log(
    request: Request,
    limit:    int = 200,
    offset:   int = 0,
    username: Optional[str] = None,
    action:   Optional[str] = None,
    date_from:Optional[str] = None,
    date_to:  Optional[str] = None,
    current_user=Depends(get_current_user)
):
    if current_user.get("role") not in ["admin", "manager"]:
        raise HTTPException(403, "Manager or Admin required")

    db = get_db()
    q = "SELECT * FROM access_log WHERE 1=1"
    params = []
    if username:  q += " AND username LIKE ?";      params.append(f"%{username}%")
    if action:    q += " AND action LIKE ?";         params.append(f"%{action}%")
    if date_from: q += " AND date(created_at) >= ?"; params.append(date_from)
    if date_to:   q += " AND date(created_at) <= ?"; params.append(date_to)
    q += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    params += [limit, offset]

    rows = db.execute(q, params).fetchall()
    total = db.execute("SELECT COUNT(*) FROM access_log").fetchone()[0]
    db.close()
    return {
        "entries": [dict(r) for r in rows],
        "total": total,
        "limit": limit,
        "offset": offset
    }

@router.get("/stats")
async def log_stats(current_user=Depends(get_current_user)):
    if current_user.get("role") not in ["admin", "manager"]:
        raise HTTPException(403, "Manager or Admin required")
    db = get_db()
    today = datetime.date.today().isoformat()
    total      = db.execute("SELECT COUNT(*) FROM access_log").fetchone()[0]
    today_n    = db.execute("SELECT COUNT(*) FROM access_log WHERE date(created_at)=?", (today,)).fetchone()[0]
    failed     = db.execute("SELECT COUNT(*) FROM access_log WHERE status='failed'").fetchone()[0]
    unique_u   = db.execute("SELECT COUNT(DISTINCT username) FROM access_log").fetchone()[0]
    last_login = db.execute("SELECT username, created_at FROM access_log WHERE action='login' AND status='success' ORDER BY created_at DESC LIMIT 1").fetchone()
    db.close()
    return {
        "total_entries": total,
        "today_entries": today_n,
        "failed_attempts": failed,
        "unique_users": unique_u,
        "last_login_user": last_login["username"] if last_login else "—",
        "last_login_time": last_login["created_at"] if last_login else "—",
    }

@router.delete("/clear")
async def clear_log(
    request: Request,
    days_older_than: int = 90,
    current_user=Depends(get_current_user)
):
    """Admin only — purge entries older than N days."""
    if current_user.get("role") != "admin":
        raise HTTPException(403, "Admin only")
    db = get_db()
    cutoff = (datetime.date.today() - datetime.timedelta(days=days_older_than)).isoformat()
    result = db.execute("DELETE FROM access_log WHERE date(created_at) < ?", (cutoff,))
    deleted = result.rowcount
    db.commit(); db.close()
    # Log the clear action itself
    log_action(request, "log_cleared",
               f"Cleared entries older than {days_older_than} days ({deleted} rows)",
               user=current_user)
    return {"deleted": deleted, "cutoff_date": cutoff}

@router.delete("/{entry_id}")
async def delete_entry(entry_id: int, request: Request,
                        current_user=Depends(get_current_user)):
    """Admin only — delete a single log entry."""
    if current_user.get("role") != "admin":
        raise HTTPException(403, "Admin only")
    db = get_db()
    db.execute("DELETE FROM access_log WHERE id=?", (entry_id,))
    db.commit(); db.close()
    log_action(request, "log_entry_deleted", f"Deleted entry #{entry_id}", user=current_user)
    return {"deleted": True}
