"""
Neural POS — Inventory Extras
Expiry date tracking, slow-mover alerts, budget vs actual, PIN clock-in,
demand forecasting, upsell suggestions
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
from models.database import get_db
from routers.auth import get_current_user
import datetime, json

router = APIRouter()

# ── Expiry tracking ───────────────────────────────────────────────────────────

class ExpiryBatch(BaseModel):
    product_id: int
    batch_number: Optional[str] = ""
    quantity: int
    expiry_date: str
    notes: Optional[str] = ""


@router.get("/expiry")
async def list_expiry(days_ahead: int = 30, current_user=Depends(get_current_user)):
    db = get_db()
    cutoff = (datetime.date.today() + datetime.timedelta(days=days_ahead)).isoformat()
    today  = datetime.date.today().isoformat()
    rows = db.execute("""
        SELECT eb.*, p.name as product_name_live
        FROM expiry_batches eb
        LEFT JOIN products p ON eb.product_id=p.id
        WHERE eb.status='active' AND eb.expiry_date <= ?
        ORDER BY eb.expiry_date ASC
    """, (cutoff,)).fetchall()
    result = []
    for r in rows:
        b = dict(r)
        b["days_until_expiry"] = (datetime.date.fromisoformat(b["expiry_date"]) - datetime.date.today()).days
        b["expired"] = b["days_until_expiry"] < 0
        result.append(b)
    db.close()
    return result

@router.post("/expiry")
async def add_expiry_batch(data: ExpiryBatch, current_user=Depends(get_current_user)):
    db = get_db()
    prod = db.execute("SELECT name FROM products WHERE id=?", (data.product_id,)).fetchone()
    if not prod: db.close(); raise HTTPException(404, "Product not found")
    uid = current_user.get("user_id") or current_user.get("id")
    db.execute("""INSERT INTO expiry_batches
        (product_id,product_name,batch_number,quantity,expiry_date,notes,created_by)
        VALUES (?,?,?,?,?,?,?)""",
        (data.product_id, prod["name"], data.batch_number or "",
         data.quantity, data.expiry_date, data.notes or "", uid))
    db.commit(); db.close()
    return {"success": True}

@router.delete("/expiry/{batch_id}")
async def remove_expiry_batch(batch_id: int, current_user=Depends(get_current_user)):
    db = get_db()
    db.execute("UPDATE expiry_batches SET status='removed' WHERE id=?", (batch_id,))
    db.commit(); db.close()
    return {"deleted": True}

# ── Slow-mover alerts ─────────────────────────────────────────────────────────

@router.get("/slow-movers")
async def slow_movers(days: int = 30, current_user=Depends(get_current_user)):
    """Products with zero or very low sales in last N days."""
    db = get_db()
    cutoff = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    rows = db.execute("""
        SELECT p.id, p.sku, p.name, p.category, p.stock_quantity, p.price, p.cost,
               COALESCE(SUM(oi.quantity), 0) as units_sold,
               COALESCE(SUM(oi.total_price), 0) as revenue,
               s.name as supplier_name
        FROM products p
        LEFT JOIN order_items oi ON p.id=oi.product_id
            AND oi.order_id IN (SELECT id FROM orders WHERE date(created_at)>=? AND status='completed')
        LEFT JOIN suppliers s ON p.supplier_id=s.id
        WHERE p.is_active=1 AND p.stock_quantity > 0
        GROUP BY p.id
        HAVING units_sold < 3
        ORDER BY units_sold ASC, p.stock_quantity DESC
        LIMIT 30
    """, (cutoff,)).fetchall()
    db.close()
    return [dict(r) for r in rows]

# ── Upsell suggestions ────────────────────────────────────────────────────────

@router.get("/upsell/{product_id}")
async def upsell_suggestions(product_id: int, current_user=Depends(get_current_user)):
    """Products frequently bought together with this product."""
    db = get_db()
    rows = db.execute("""
        SELECT oi2.product_id, oi2.product_name,
               COUNT(*) as co_purchases,
               p.price, p.stock_quantity
        FROM order_items oi1
        JOIN order_items oi2 ON oi1.order_id=oi2.order_id AND oi2.product_id != oi1.product_id
        LEFT JOIN products p ON oi2.product_id=p.id
        WHERE oi1.product_id=? AND p.is_active=1 AND p.stock_quantity > 0
        GROUP BY oi2.product_id
        ORDER BY co_purchases DESC
        LIMIT 5
    """, (product_id,)).fetchall()
    db.close()
    return [dict(r) for r in rows]

# ── Budget vs actual ──────────────────────────────────────────────────────────

class BudgetTarget(BaseModel):
    period: str       # YYYY-MM
    category: str
    target_amount: float

@router.get("/budget")
async def get_budget(period: Optional[str] = None, current_user=Depends(get_current_user)):
    db = get_db()
    p = period or datetime.date.today().strftime("%Y-%m")
    targets = db.execute("SELECT * FROM budget_targets WHERE period=?", (p,)).fetchall()
    actuals = db.execute("""
        SELECT category, COALESCE(SUM(amount),0) as actual
        FROM expenses WHERE strftime('%Y-%m',date)=?
        GROUP BY category
    """, (p,)).fetchall()
    actual_map = {r[0]: r[1] for r in actuals}
    revenue = db.execute("""
        SELECT COALESCE(SUM(total),0) FROM orders
        WHERE strftime('%Y-%m',created_at)=? AND status='completed'
    """, (p,)).fetchone()[0]
    db.close()
    result = []
    for t in targets:
        cat = t["category"]
        actual = actual_map.get(cat, 0)
        result.append({
            "id": t["id"], "period": p, "category": cat,
            "target": t["target_amount"], "actual": actual,
            "variance": actual - t["target_amount"],
            "pct_used": round(actual / t["target_amount"] * 100, 1) if t["target_amount"] > 0 else 0
        })
    # Add unbudgeted categories
    for cat, actual in actual_map.items():
        if not any(r["category"]==cat for r in result):
            result.append({"id": None, "period": p, "category": cat,
                           "target": 0, "actual": actual, "variance": actual, "pct_used": 100})
    return {"period": p, "revenue": revenue, "items": result}

@router.post("/budget")
async def set_budget(data: BudgetTarget, current_user=Depends(get_current_user)):
    if current_user.get("role") not in ["admin","manager"]:
        raise HTTPException(403, "Manager or Admin required")
    db = get_db()
    db.execute("""INSERT OR REPLACE INTO budget_targets (period,category,target_amount)
                  VALUES (?,?,?)""", (data.period, data.category, data.target_amount))
    db.commit(); db.close()
    return {"success": True}

# ── PIN clock-in ─────────────────────────────────────────────────────────────

import hashlib

class PINSetup(BaseModel):
    pin: str       # 4-digit PIN

class PINClockIn(BaseModel):
    user_id: int
    pin: str
    action: str    # "in" or "out"

@router.post("/pin/setup")
async def setup_pin(data: PINSetup, current_user=Depends(get_current_user)):
    if len(data.pin) != 4 or not data.pin.isdigit():
        raise HTTPException(400, "PIN must be exactly 4 digits")
    uid = current_user.get("user_id") or current_user.get("id")
    pin_hash = hashlib.sha256(data.pin.encode()).hexdigest()
    db = get_db()
    db.execute("INSERT OR REPLACE INTO staff_pins (user_id,pin_hash,updated_at) VALUES (?,?,datetime('now'))",
               (uid, pin_hash))
    db.commit(); db.close()
    return {"success": True, "message": "PIN set successfully"}

@router.post("/pin/clockin")
async def pin_clock_in(data: PINClockIn, current_user=Depends(get_current_user)):
    """Verify PIN and log shift start/end."""
    pin_hash = hashlib.sha256(data.pin.encode()).hexdigest()
    db = get_db()
    row = db.execute("SELECT * FROM staff_pins WHERE user_id=? AND pin_hash=?",
                     (data.user_id, pin_hash)).fetchone()
    if not row:
        db.close(); raise HTTPException(401, "Invalid PIN")

    user = db.execute("SELECT full_name, role FROM users WHERE id=?", (data.user_id,)).fetchone()
    today = datetime.date.today().isoformat()
    now_time = datetime.datetime.now().strftime("%H:%M")

    if data.action == "in":
        # Check not already clocked in
        existing = db.execute(
            "SELECT id FROM shifts WHERE user_id=? AND shift_date=? AND status='active'",
            (data.user_id, today)).fetchone()
        if existing:
            db.close()
            return {"success": True, "message": f"Already clocked in — welcome back, {user['full_name']}!"}
        db.execute("INSERT INTO shifts (user_id,shift_date,start_time,status) VALUES (?,?,?,'active')",
                   (data.user_id, today, now_time))
        msg = f"Clock-in confirmed — {user['full_name']} at {now_time}"
    else:  # out
        shift = db.execute(
            "SELECT id, start_time FROM shifts WHERE user_id=? AND shift_date=? AND status='active'",
            (data.user_id, today)).fetchone()
        if not shift:
            db.close(); return {"success": False, "message": "No active shift found"}
        try:
            s = datetime.datetime.strptime(shift["start_time"], "%H:%M")
            e = datetime.datetime.strptime(now_time, "%H:%M")
            hours = round((e - s).seconds / 3600, 2)
        except: hours = 0
        db.execute("UPDATE shifts SET end_time=?,hours_worked=?,status='completed' WHERE id=?",
                   (now_time, hours, shift["id"]))
        msg = f"Clock-out confirmed — {user['full_name']} worked {hours}h"

    db.commit(); db.close()
    return {"success": True, "message": msg, "staff_name": user["full_name"], "time": now_time}

# ── Demand forecasting ────────────────────────────────────────────────────────

@router.get("/forecast")
async def demand_forecast(current_user=Depends(get_current_user)):
    """Simple demand forecast based on last 4 weeks sales patterns."""
    db = get_db()
    rows = db.execute("""
        SELECT p.id, p.name, p.category, p.stock_quantity, p.reorder_level, p.cost,
               COALESCE(SUM(oi.quantity), 0) as sold_30d,
               COALESCE(SUM(CASE WHEN date(o.created_at) >= date('now','-7 days')
                           THEN oi.quantity ELSE 0 END), 0) as sold_7d
        FROM products p
        LEFT JOIN order_items oi ON p.id=oi.product_id
        LEFT JOIN orders o ON oi.order_id=o.id AND o.status='completed'
            AND date(o.created_at) >= date('now','-30 days')
        WHERE p.is_active=1
        GROUP BY p.id
        HAVING sold_30d > 0
        ORDER BY sold_30d DESC
        LIMIT 20
    """).fetchall()
    db.close()

    result = []
    for r in rows:
        r = dict(r)
        daily_rate = r["sold_30d"] / 30
        days_stock  = round(r["stock_quantity"] / daily_rate, 1) if daily_rate > 0 else 999
        suggest_qty = max(0, round(daily_rate * 14) - r["stock_quantity"])  # 2-week stock

        # Day-of-week pattern (simplified: if 7d > 30d/4, trending up)
        weekly_avg  = r["sold_30d"] / 4
        trend = "rising" if r["sold_7d"] > weekly_avg else ("falling" if r["sold_7d"] < weekly_avg * 0.7 else "stable")

        result.append({
            "id": r["id"], "name": r["name"], "category": r["category"],
            "stock": r["stock_quantity"], "reorder_level": r["reorder_level"],
            "sold_30d": r["sold_30d"], "sold_7d": r["sold_7d"],
            "daily_rate": round(daily_rate, 2),
            "days_of_stock": days_stock,
            "trend": trend,
            "suggest_reorder": suggest_qty > 0,
            "suggested_qty": suggest_qty,
            "estimated_cost": round(suggest_qty * (r["cost"] or 0), 2),
            "urgency": "critical" if days_stock < 3 else ("warning" if days_stock < 7 else "ok")
        })

    return sorted(result, key=lambda x: x["days_of_stock"])
