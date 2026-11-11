"""
Neural POS — Expense Tracking Module
Log business costs, categorize, P&L visibility
"""
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional
from models.database import get_db
from routers.auth import get_current_user

router = APIRouter()

class ExpenseCreate(BaseModel):
    title: str
    amount: float
    category: str
    description: Optional[str] = ""
    date: Optional[str] = ""
    payment_method: Optional[str] = "cash"
    reference: Optional[str] = ""


@router.get("")
async def list_expenses(month: Optional[str] = None, category: Optional[str] = None, current_user=Depends(get_current_user)):
    db = get_db()
    q = "SELECT id,title,amount,category,description,date,payment_method,reference,created_at FROM expenses WHERE 1=1"
    params = []
    if month:
        q += " AND strftime('%Y-%m',date)=?"; params.append(month)
    if category:
        q += " AND category=?"; params.append(category)
    q += " ORDER BY date DESC"
    rows = db.execute(q, params).fetchall()
    db.close()
    keys = ["id","title","amount","category","description","date","payment_method","reference","created_at"]
    return [dict(zip(keys, r)) for r in rows]

@router.get("/summary")
async def expense_summary(current_user=Depends(get_current_user)):
    db = get_db()
    today_total   = db.execute("SELECT COALESCE(SUM(amount),0) FROM expenses WHERE date=date('now')").fetchone()[0]
    month_total   = db.execute("SELECT COALESCE(SUM(amount),0) FROM expenses WHERE strftime('%Y-%m',date)=strftime('%Y-%m','now')").fetchone()[0]
    by_category   = db.execute("SELECT category, SUM(amount) as total FROM expenses WHERE strftime('%Y-%m',date)=strftime('%Y-%m','now') GROUP BY category ORDER BY total DESC").fetchall()
    revenue_today = db.execute("SELECT COALESCE(SUM(total),0) FROM orders WHERE date(created_at)=date('now') AND status='completed'").fetchone()[0]
    revenue_month = db.execute("SELECT COALESCE(SUM(total),0) FROM orders WHERE strftime('%Y-%m',created_at)=strftime('%Y-%m','now') AND status='completed'").fetchone()[0]
    db.close()
    return {
        "today_expenses": today_total,
        "month_expenses": month_total,
        "profit_today": revenue_today - today_total,
        "profit_month": revenue_month - month_total,
        "revenue_month": revenue_month,
        "by_category": [{"category": r[0], "total": r[1]} for r in by_category]
    }

@router.post("")
async def create_expense(data: ExpenseCreate, current_user=Depends(get_current_user)):
    import datetime
    db = get_db()
    date = data.date or datetime.date.today().isoformat()
    db.execute("INSERT INTO expenses (title,amount,category,description,date,payment_method,reference,created_by) VALUES (?,?,?,?,?,?,?,?)",
               (data.title, data.amount, data.category, data.description, date, data.payment_method, data.reference, current_user["id"]))
    eid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.commit()
    row = db.execute("SELECT id,title,amount,category,description,date,payment_method,reference,created_at FROM expenses WHERE id=?", (eid,)).fetchone()
    db.close()
    return dict(zip(["id","title","amount","category","description","date","payment_method","reference","created_at"], row))

@router.put("/{expense_id}")
async def update_expense(expense_id: int, data: ExpenseCreate, current_user=Depends(get_current_user)):
    db = get_db()
    if not db.execute("SELECT id FROM expenses WHERE id=?", (expense_id,)).fetchone():
        db.close(); raise HTTPException(404, "Expense not found")
    db.execute("UPDATE expenses SET title=?,amount=?,category=?,description=?,date=?,payment_method=?,reference=? WHERE id=?",
               (data.title, data.amount, data.category, data.description, data.date, data.payment_method, data.reference, expense_id))
    db.commit(); db.close()
    return {"updated": True}

@router.delete("/{expense_id}")
async def delete_expense(expense_id: int, current_user=Depends(get_current_user)):
    db = get_db()
    db.execute("DELETE FROM expenses WHERE id=?", (expense_id,))
    db.commit(); db.close()
    return {"deleted": True}
