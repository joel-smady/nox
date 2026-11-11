"""
Neural POS — End-of-Day Cash-Up Module
Till reconciliation, shift close, daily P&L snapshot
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
from models.database import get_db
from routers.auth import get_current_user
import datetime

router = APIRouter()

class CashUpSubmit(BaseModel):
    cashier_id: Optional[int] = None
    counted_cash: float
    float_amount: Optional[float] = 200.0   # opening float
    notes: Optional[str] = ""


@router.get("/summary")
async def get_eod_summary(current_user=Depends(get_current_user)):
    """Get today's trading summary for cash-up screen."""
    db = get_db()
    uid = current_user.get("user_id") or current_user.get("id")
    role = current_user.get("role")
    today = datetime.date.today().isoformat()

    # Build query — cashiers see only their own sales
    if role == "cashier":
        filter_sql = "AND o.cashier_id = ?"
        params = [today, uid]
    else:
        filter_sql = ""
        params = [today]

    # Sales by payment method
    rows = db.execute(f"""
        SELECT payment_method,
               COUNT(*) as orders,
               COALESCE(SUM(total), 0) as revenue
        FROM orders
        WHERE date(created_at) = ? AND status = 'completed'
        {filter_sql}
        GROUP BY payment_method
    """, params).fetchall()

    summary = {"cash": 0, "mobile_money": 0, "card": 0, "split": 0,
               "total_orders": 0, "total_revenue": 0}
    for r in rows:
        method = r[0] or "cash"
        summary[method] = r[2]
        summary["total_orders"] += r[1]
        summary["total_revenue"] += r[2]

    # Refunds today
    refund_filter = "AND r.processed_by = ?" if role == "cashier" else ""
    refund_params = [today, uid] if role == "cashier" else [today]
    refunds = db.execute(f"""
        SELECT COALESCE(SUM(refund_amount),0)
        FROM returns r
        WHERE date(r.created_at) = ? {refund_filter}
    """, refund_params).fetchone()[0]

    summary["total_refunds"] = refunds
    summary["expected_cash"] = round(summary["cash"] - refunds, 2)
    summary["date"] = today

    # Check if already cashed up today
    existing = db.execute(
        "SELECT id, status, variance FROM cash_ups WHERE shift_date=? AND (cashier_id=? OR ? IS NOT ?)",
        (today, uid, role, "cashier")
    ).fetchone()
    summary["already_cashed_up"] = bool(existing and existing["status"] == "closed")

    db.close()
    return summary

@router.post("/submit")
async def submit_cash_up(data: CashUpSubmit, current_user=Depends(get_current_user)):
    """Submit end-of-day cash-up."""
    db = get_db()
    uid = current_user.get("user_id") or current_user.get("id")
    role = current_user.get("role")
    today = datetime.date.today().isoformat()

    # Gather today's figures
    filter_sql = "AND cashier_id=?" if role == "cashier" else ""
    params_cash = [today, uid] if role == "cashier" else [today]

    rows = db.execute(f"""
        SELECT payment_method, COUNT(*) as cnt, COALESCE(SUM(total),0) as rev
        FROM orders WHERE date(created_at)=? AND status='completed' {filter_sql}
        GROUP BY payment_method
    """, params_cash).fetchall()

    cash_s = mpesa_s = card_s = split_s = 0
    total_orders = 0
    for r in rows:
        m = r[0] or "cash"
        if m == "cash":          cash_s  = r[2]
        elif m == "mobile_money": mpesa_s = r[2]
        elif m == "card":         card_s  = r[2]
        elif m == "split":        split_s = r[2]
        total_orders += r[1]

    refunds = db.execute(
        "SELECT COALESCE(SUM(refund_amount),0) FROM returns WHERE date(created_at)=?", (today,)
    ).fetchone()[0]

    expected = round(cash_s - refunds + (data.float_amount or 0), 2)
    variance = round(data.counted_cash - expected, 2)
    cashier_name = current_user.get("full_name") or "Unknown"

    db.execute("""
        INSERT INTO cash_ups
        (shift_date, cashier_id, cashier_name, expected_cash, counted_cash, float_amount,
         variance, cash_sales, mpesa_sales, card_sales, split_sales,
         total_sales, total_orders, total_refunds, notes, status, closed_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'closed',datetime('now'))
    """, (today, uid, cashier_name, expected, data.counted_cash,
          data.float_amount or 0, variance, cash_s, mpesa_s, card_s, split_s,
          cash_s+mpesa_s+card_s+split_s, total_orders, refunds, data.notes or ""))
    db.commit(); db.close()

    status_label = "BALANCED" if abs(variance) < 1 else ("SURPLUS" if variance > 0 else "SHORTAGE")
    return {
        "success": True, "variance": variance,
        "status": status_label,
        "message": f"Cash-up complete. Variance: KES {variance:+,.2f} ({status_label})"
    }

@router.get("/history")
async def cash_up_history(current_user=Depends(get_current_user)):
    db = get_db()
    rows = db.execute("""
        SELECT shift_date, cashier_name, expected_cash, counted_cash, variance,
               total_sales, total_orders, status, closed_at
        FROM cash_ups ORDER BY shift_date DESC LIMIT 30
    """).fetchall()
    db.close()
    keys = ["shift_date","cashier_name","expected_cash","counted_cash","variance",
            "total_sales","total_orders","status","closed_at"]
    return [dict(zip(keys, r)) for r in rows]
