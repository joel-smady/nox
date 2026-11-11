"""
Neural POS — Admin Router
System administration, user management
"""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
from models.database import get_db, hash_password
from routers.auth import get_current_user

router = APIRouter()

class UserCreate(BaseModel):
    username: str
    password: str
    full_name: str
    role: str
    email: Optional[str] = None

class UserUpdate(BaseModel):
    full_name: Optional[str] = None
    role: Optional[str] = None
    email: Optional[str] = None
    is_active: Optional[int] = None
    password: Optional[str] = None

def require_admin(request: Request):
    user = get_current_user(request)
    if not user or user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user

@router.get("/users")
async def list_users(request: Request):
    require_admin(request)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id, username, full_name, role, email, is_active, created_at, last_login FROM users ORDER BY role, full_name")
    users = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return users

@router.post("/users")
async def create_user(user: UserCreate, request: Request):
    require_admin(request)
    if user.role not in ["admin", "manager", "cashier"]:
        raise HTTPException(status_code=400, detail="Invalid role")
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO users (username, password_hash, full_name, role, email)
            VALUES (?, ?, ?, ?, ?)
        """, (user.username, hash_password(user.password), user.full_name, user.role, user.email))
        uid = cursor.lastrowid
        conn.commit()
        conn.close()
        return {"success": True, "user_id": uid}
    except Exception as e:
        conn.close()
        raise HTTPException(status_code=400, detail="Username already exists")

@router.put("/users/{user_id}")
async def update_user(user_id: int, user: UserUpdate, request: Request):
    require_admin(request)
    conn = get_db()
    cursor = conn.cursor()
    updates = {}
    if user.full_name: updates["full_name"] = user.full_name
    if user.role: updates["role"] = user.role
    if user.email: updates["email"] = user.email
    if user.is_active is not None: updates["is_active"] = user.is_active
    if user.password: updates["password_hash"] = hash_password(user.password)

    if updates:
        set_clause = ", ".join([f"{k} = ?" for k in updates.keys()])
        cursor.execute(f"UPDATE users SET {set_clause} WHERE id = ?", list(updates.values()) + [user_id])
        conn.commit()
    conn.close()
    return {"success": True}

@router.delete("/users/{user_id}")
async def delete_user(user_id: int, request: Request):
    admin = require_admin(request)
    if admin["user_id"] == user_id:
        raise HTTPException(status_code=400, detail="Cannot deactivate your own account")
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET is_active = 0 WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()
    return {"success": True}

@router.get("/system-stats")
async def system_stats(request: Request):
    require_admin(request)
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) FROM users WHERE is_active = 1")
    users = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM products WHERE is_active = 1")
    products = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM orders")
    orders = cursor.fetchone()[0]
    cursor.execute("SELECT COALESCE(SUM(total), 0) FROM orders")
    revenue = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM suppliers WHERE is_active = 1")
    suppliers = cursor.fetchone()[0]

    conn.close()
    return {
        "total_users": users,
        "total_products": products,
        "total_orders": orders,
        "total_revenue": round(revenue, 2),
        "total_suppliers": suppliers,
        "system_version": "9.0.0",
        "db_engine": "SQLite (WAL mode)",
        "ai_model": "Ollama (configurable)"
    }

@router.get("/audit-log")
async def audit_log(request: Request):
    require_admin(request)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT o.order_number, o.total, o.payment_method, o.created_at,
               u.full_name as cashier
        FROM orders o LEFT JOIN users u ON o.cashier_id = u.id
        ORDER BY o.created_at DESC LIMIT 100
    """)
    logs = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return logs
