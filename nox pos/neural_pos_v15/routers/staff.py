"""
Neural POS — Staff Management Module
Shifts, attendance, commissions, performance
"""
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional
from models.database import get_db
from routers.auth import get_current_user
import datetime

router = APIRouter()

class ShiftCreate(BaseModel):
    user_id: int
    shift_date: str
    start_time: str
    end_time: Optional[str] = None
    notes: Optional[str] = ""


@router.get("/shifts")
async def list_shifts(user_id: Optional[int] = None, date: Optional[str] = None, current_user=Depends(get_current_user)):
    db = get_db()
    q = """SELECT s.id, s.user_id, u.full_name, u.role, s.shift_date, s.start_time, s.end_time,
                  s.hours_worked, s.notes, s.status
           FROM shifts s JOIN users u ON s.user_id=u.id WHERE 1=1"""
    params = []
    if user_id:
        q += " AND s.user_id=?"; params.append(user_id)
    if date:
        q += " AND s.shift_date=?"; params.append(date)
    q += " ORDER BY s.shift_date DESC, s.start_time DESC LIMIT 100"
    rows = db.execute(q, params).fetchall()
    db.close()
    keys = ["id","user_id","full_name","role","shift_date","start_time","end_time","hours_worked","notes","status"]
    return [dict(zip(keys, r)) for r in rows]

@router.post("/shifts")
async def create_shift(data: ShiftCreate, current_user=Depends(get_current_user)):
    db = get_db()
    hours = 0
    if data.end_time:
        try:
            s = datetime.datetime.strptime(data.start_time, "%H:%M")
            e = datetime.datetime.strptime(data.end_time, "%H:%M")
            hours = round((e - s).seconds / 3600, 2)
        except:
            hours = 0
    status = "completed" if data.end_time else "active"
    db.execute("INSERT INTO shifts (user_id,shift_date,start_time,end_time,hours_worked,notes,status) VALUES (?,?,?,?,?,?,?)",
               (data.user_id, data.shift_date, data.start_time, data.end_time, hours, data.notes, status))
    db.commit(); db.close()
    return {"created": True}

@router.put("/shifts/{shift_id}/close")
async def close_shift(shift_id: int, body: dict, current_user=Depends(get_current_user)):
    db = get_db()
    shift = db.execute("SELECT start_time FROM shifts WHERE id=?", (shift_id,)).fetchone()
    if not shift:
        db.close(); raise HTTPException(404, "Shift not found")
    end_time = body.get("end_time", "17:00")
    try:
        s = datetime.datetime.strptime(shift[0], "%H:%M")
        e = datetime.datetime.strptime(end_time, "%H:%M")
        hours = round((e - s).seconds / 3600, 2)
    except:
        hours = 0
    db.execute("UPDATE shifts SET end_time=?,hours_worked=?,status='completed' WHERE id=?", (end_time, hours, shift_id))
    db.commit(); db.close()
    return {"hours_worked": hours}

@router.get("/performance")
async def staff_performance(period: Optional[str] = None, current_user=Depends(get_current_user)):
    db = get_db()
    period_filter = period or datetime.date.today().strftime("%Y-%m")
    rows = db.execute("""
        SELECT u.id, u.full_name, u.role,
               COUNT(o.id) as orders,
               COALESCE(SUM(o.total),0) as revenue,
               COALESCE(SUM(s.hours_worked),0) as hours
        FROM users u
        LEFT JOIN orders o ON o.cashier_id=u.id AND strftime('%Y-%m',o.created_at)=?
        LEFT JOIN shifts s ON s.user_id=u.id AND strftime('%Y-%m',s.shift_date)=?
        WHERE u.is_active=1
        GROUP BY u.id ORDER BY revenue DESC
    """, (period_filter, period_filter)).fetchall()
    db.close()
    return [{
        "id": r[0], "full_name": r[1], "role": r[2],
        "orders": r[3], "revenue": r[4], "hours_worked": r[5],
        "commission": round(r[4] * 0.025, 2)
    } for r in rows]

@router.get("/summary")
async def staff_summary(current_user=Depends(get_current_user)):
    db = get_db()
    total_staff = db.execute("SELECT COUNT(*) FROM users WHERE is_active=1").fetchone()[0]
    on_shift    = db.execute("SELECT COUNT(DISTINCT user_id) FROM shifts WHERE shift_date=date('now') AND status='active'").fetchone()[0]
    month_hours = db.execute("SELECT COALESCE(SUM(hours_worked),0) FROM shifts WHERE strftime('%Y-%m',shift_date)=strftime('%Y-%m','now')").fetchone()[0]
    db.close()
    return {"total_staff": total_staff, "on_shift_today": on_shift, "month_hours": month_hours}
