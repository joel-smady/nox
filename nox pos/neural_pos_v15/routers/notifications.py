"""
Neural POS — Notifications & Alerts Module
Smart alerts: low stock, unusual refunds, daily summary
"""
from fastapi import APIRouter, Depends
from models.database import get_db
from routers.auth import get_current_user

router = APIRouter()


def generate_alerts():
    """Scan live data and produce alert objects — called on demand."""
    db = get_db()
    alerts = []

    # 1. Low stock
    low = db.execute("""
        SELECT name, stock_quantity, reorder_level FROM products
        WHERE is_active=1 AND stock_quantity <= reorder_level ORDER BY stock_quantity ASC LIMIT 10
    """).fetchall()
    for p in low:
        severity = "error" if p[1] == 0 else "warning"
        alerts.append({
            "type": "low_stock", "severity": severity,
            "title": "Low Stock" if p[1] > 0 else "Out of Stock",
            "message": f"{p[0]} — {p[1]} left (reorder at {p[2]})",
            "link": "inventory"
        })

    # 2. Overdue invoices
    overdue = db.execute("SELECT COUNT(*) FROM invoices WHERE status='overdue'").fetchone()[0]
    if overdue:
        alerts.append({
            "type": "invoice", "severity": "warning",
            "title": f"{overdue} Overdue Invoice{'s' if overdue>1 else ''}",
            "message": "Some invoices are past their due date.",
            "link": "invoices"
        })

    # 3. Outstanding credit
    credit = db.execute("SELECT COUNT(*), COALESCE(SUM(credit_balance),0) FROM customers WHERE credit_balance>0").fetchall()[0]
    if credit[0]:
        alerts.append({
            "type": "credit", "severity": "info",
            "title": f"KES {credit[1]:,.0f} Outstanding Credit",
            "message": f"{credit[0]} customer(s) have unpaid credit/deni balances.",
            "link": "customers"
        })

    # 4. High refunds today
    today_refunds = db.execute(
        "SELECT COALESCE(SUM(refund_amount),0) FROM returns WHERE date(created_at)=date('now')"
    ).fetchone()[0]
    today_sales = db.execute(
        "SELECT COALESCE(SUM(total),0) FROM orders WHERE date(created_at)=date('now') AND status='completed'"
    ).fetchone()[0]
    if today_sales > 0 and today_refunds / today_sales > 0.15:
        alerts.append({
            "type": "refunds", "severity": "error",
            "title": "High Refund Rate Today",
            "message": f"Refunds are {(today_refunds/today_sales*100):.0f}% of today's sales — review activity.",
            "link": "returns"
        })

    # 5. Today's summary (always included)
    revenue = db.execute(
        "SELECT COALESCE(SUM(total),0), COUNT(*) FROM orders WHERE date(created_at)=date('now') AND status='completed'"
    ).fetchone()
    alerts.append({
        "type": "summary", "severity": "info",
        "title": "Today's Performance",
        "message": f"{revenue[1]} orders · KES {revenue[0]:,.0f} revenue",
        "link": "overview"
    })

    db.close()
    return alerts

@router.get("")
async def get_notifications(current_user=Depends(get_current_user)):
    return generate_alerts()

@router.get("/count")
async def notification_count(current_user=Depends(get_current_user)):
    alerts = generate_alerts()
    urgent = sum(1 for a in alerts if a["severity"] in ("error", "warning"))
    return {"total": len(alerts), "urgent": urgent}
