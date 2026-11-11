"""
Neural POS — Smart Alerts & Daily Summary
Auto reorder alerts, daily P&L email, configurable triggers
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
from models.database import get_db
from routers.auth import get_current_user
import datetime, json

router = APIRouter()

class AlertConfig(BaseModel):
    email_recipient: Optional[str] = ""
    daily_summary_enabled: bool = True
    daily_summary_time: str = "20:00"
    reorder_alerts_enabled: bool = True
    low_stock_threshold_pct: int = 20   # alert when stock < X% of reorder level
    overdue_credit_alerts: bool = True


@router.get("/config")
async def get_config(current_user=Depends(get_current_user)):
    db = get_db()
    row = db.execute("SELECT * FROM alert_config WHERE id=1").fetchone()
    db.close()
    if not row: return {}
    return dict(row)

@router.put("/config")
async def update_config(data: AlertConfig, current_user=Depends(get_current_user)):
    if current_user.get("role") not in ["admin","manager"]:
        raise HTTPException(403, "Manager or Admin required")
    db = get_db()
    db.execute("""
        UPDATE alert_config SET
            email_recipient=?, daily_summary_enabled=?, daily_summary_time=?,
            reorder_alerts_enabled=?, low_stock_threshold_pct=?,
            overdue_credit_alerts=?, updated_at=datetime('now')
        WHERE id=1
    """, (data.email_recipient, int(data.daily_summary_enabled),
          data.daily_summary_time, int(data.reorder_alerts_enabled),
          data.low_stock_threshold_pct, int(data.overdue_credit_alerts)))
    db.commit(); db.close()
    return {"updated": True}

@router.get("/check")
async def check_alerts(current_user=Depends(get_current_user)):
    """Run all alert checks and return current alerts."""
    db = get_db()
    alerts = []

    # 1. Low stock / out of stock
    low = db.execute("""
        SELECT name, stock_quantity, reorder_level
        FROM products WHERE is_active=1 AND stock_quantity <= reorder_level
        ORDER BY stock_quantity ASC LIMIT 15
    """).fetchall()
    for p in low:
        sev = "critical" if p[1] == 0 else "warning"
        alerts.append({"type": "low_stock", "severity": sev,
                        "title": f"{'OUT OF STOCK' if p[1]==0 else 'Low stock'}: {p[0]}",
                        "message": f"{p[1]} units left (reorder at {p[2]})",
                        "action": "inventory"})

    # 2. Overdue credit sales
    overdue = db.execute("""
        SELECT cs.sale_number, cs.customer_name, cs.balance, cs.due_date
        FROM credit_sales cs WHERE cs.status='overdue'
        ORDER BY cs.due_date ASC LIMIT 10
    """).fetchall()
    for o in overdue:
        alerts.append({"type": "credit_overdue", "severity": "warning",
                        "title": f"Overdue credit: {o[1]}",
                        "message": f"{o[0]} — KES {o[2]:,.0f} due {o[3]}",
                        "action": "credit"})

    # 3. Today's performance snapshot
    today = datetime.date.today().isoformat()
    perf = db.execute("""
        SELECT COUNT(*), COALESCE(SUM(total),0) FROM orders
        WHERE date(created_at)=? AND status='completed'
    """, (today,)).fetchone()
    alerts.append({"type": "summary", "severity": "info",
                    "title": "Today's performance",
                    "message": f"{perf[0]} orders · KES {perf[1]:,.0f} revenue",
                    "action": "overview"})

    # 4. High refund rate
    refunds_today = db.execute("SELECT COALESCE(SUM(refund_amount),0) FROM returns WHERE date(created_at)=?", (today,)).fetchone()[0]
    if perf[1] > 0 and refunds_today / perf[1] > 0.15:
        alerts.append({"type": "high_refunds", "severity": "critical",
                        "title": "High refund rate today",
                        "message": f"Refunds are {refunds_today/perf[1]*100:.0f}% of sales. Review activity.",
                        "action": "returns"})

    # 5. Uncashed-up shifts
    cashed = db.execute("SELECT COUNT(*) FROM cash_ups WHERE shift_date=? AND status='closed'", (today,)).fetchone()[0]
    if perf[0] > 0 and cashed == 0:
        alerts.append({"type": "cashup_pending", "severity": "warning",
                        "title": "Cash-up not done",
                        "message": "Today's shift has not been reconciled yet.",
                        "action": "eod"})

    db.close()
    return {"alerts": alerts, "count": len(alerts),
            "critical": sum(1 for a in alerts if a["severity"] == "critical"),
            "warning": sum(1 for a in alerts if a["severity"] == "warning")}

@router.get("/daily-summary")
async def daily_summary(date: Optional[str] = None, current_user=Depends(get_current_user)):
    """Generate a full daily business summary."""
    db = get_db()
    d = date or datetime.date.today().isoformat()

    orders  = db.execute("SELECT COUNT(*), COALESCE(SUM(total),0), COALESCE(SUM(discount),0), COALESCE(SUM(tax),0) FROM orders WHERE date(created_at)=? AND status='completed'", (d,)).fetchone()
    by_pay  = db.execute("SELECT payment_method, COUNT(*), COALESCE(SUM(total),0) FROM orders WHERE date(created_at)=? AND status='completed' GROUP BY payment_method", (d,)).fetchall()
    top5    = db.execute("SELECT oi.product_name, SUM(oi.quantity) as qty, SUM(oi.total_price) as rev FROM order_items oi JOIN orders o ON oi.order_id=o.id WHERE date(o.created_at)=? GROUP BY oi.product_name ORDER BY rev DESC LIMIT 5", (d,)).fetchall()
    exp     = db.execute("SELECT COALESCE(SUM(amount),0) FROM expenses WHERE date=?", (d,)).fetchone()[0]
    refunds = db.execute("SELECT COUNT(*), COALESCE(SUM(refund_amount),0) FROM returns WHERE date(created_at)=?", (d,)).fetchall()[0]
    cashup  = db.execute("SELECT variance, status FROM cash_ups WHERE shift_date=? LIMIT 1", (d,)).fetchone()
    cogs    = db.execute("SELECT COALESCE(SUM(oi.quantity*p.cost),0) FROM order_items oi JOIN orders o ON oi.order_id=o.id JOIN products p ON oi.product_id=p.id WHERE date(o.created_at)=? AND o.status='completed'", (d,)).fetchone()[0]

    gross_profit = (orders[1] or 0) - cogs - (refunds[1] or 0)
    net_profit   = gross_profit - (exp or 0)

    db.close()
    return {
        "date": d,
        "orders": {"count": orders[0], "revenue": orders[1], "discounts": orders[2], "vat": orders[3]},
        "by_payment": [{"method": r[0], "count": r[1], "amount": r[2]} for r in by_pay],
        "top_products": [{"name": r[0], "qty": r[1], "revenue": r[2]} for r in top5],
        "expenses": exp,
        "refunds": {"count": refunds[0], "amount": refunds[1]},
        "cogs": cogs,
        "gross_profit": gross_profit,
        "net_profit": net_profit,
        "cash_up": {"variance": cashup[0] if cashup else None, "status": cashup[1] if cashup else "not done"}
    }
