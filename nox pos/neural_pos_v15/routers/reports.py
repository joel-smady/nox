"""
Neural POS — Reports & Export Module
KRA-friendly tax reports, P&L, CSV/JSON export
"""
from fastapi import APIRouter, Depends
from fastapi.responses import Response
from models.database import get_db
from routers.auth import get_current_user
import csv, io, json
from datetime import datetime

router = APIRouter()

@router.get("/tax")
async def tax_report(month: str = None, current_user=Depends(get_current_user)):
    """KRA-friendly VAT summary report."""
    db = get_db()
    period = month or datetime.now().strftime("%Y-%m")
    rows = db.execute("""
        SELECT date(created_at) as sale_date,
               COUNT(*) as transactions,
               COALESCE(SUM(subtotal),0) as taxable_amount,
               COALESCE(SUM(tax),0) as vat_collected,
               COALESCE(SUM(total),0) as gross_total
        FROM orders
        WHERE strftime('%Y-%m', created_at)=? AND status='completed'
        GROUP BY date(created_at)
        ORDER BY sale_date
    """, (period,)).fetchall()
    totals = db.execute("""
        SELECT COALESCE(SUM(subtotal),0), COALESCE(SUM(tax),0), COALESCE(SUM(total),0), COUNT(*)
        FROM orders WHERE strftime('%Y-%m',created_at)=? AND status='completed'
    """, (period,)).fetchone()
    db.close()
    return {
        "period": period, "vat_rate": 16,
        "daily": [{"date":r[0],"transactions":r[1],"taxable":r[2],"vat":r[3],"gross":r[4]} for r in rows],
        "totals": {"taxable": totals[0], "vat_collected": totals[1], "gross": totals[2], "transactions": totals[3]}
    }

@router.get("/pl")
async def profit_loss(month: str = None, current_user=Depends(get_current_user)):
    """Full Profit & Loss statement."""
    db = get_db()
    period = month or datetime.now().strftime("%Y-%m")
    revenue = db.execute("SELECT COALESCE(SUM(total),0) FROM orders WHERE strftime('%Y-%m',created_at)=? AND status='completed'", (period,)).fetchone()[0]
    tax_col  = db.execute("SELECT COALESCE(SUM(tax),0) FROM orders WHERE strftime('%Y-%m',created_at)=? AND status='completed'", (period,)).fetchone()[0]
    discounts= db.execute("SELECT COALESCE(SUM(discount),0) FROM orders WHERE strftime('%Y-%m',created_at)=? AND status='completed'", (period,)).fetchone()[0]
    refunds  = db.execute("SELECT COALESCE(SUM(refund_amount),0) FROM returns WHERE strftime('%Y-%m',created_at)=?", (period,)).fetchone()[0]
    expenses = db.execute("SELECT COALESCE(SUM(amount),0) FROM expenses WHERE strftime('%Y-%m',date)=?", (period,)).fetchone()[0]
    exp_cats = db.execute("SELECT category, SUM(amount) FROM expenses WHERE strftime('%Y-%m',date)=? GROUP BY category", (period,)).fetchall()

    # COGS from order items + product cost
    cogs_row = db.execute("""
        SELECT COALESCE(SUM(oi.quantity * p.cost), 0)
        FROM order_items oi
        JOIN orders o ON oi.order_id=o.id
        JOIN products p ON oi.product_id=p.id
        WHERE strftime('%Y-%m', o.created_at)=? AND o.status='completed'
    """, (period,)).fetchone()[0]

    gross_profit = revenue - cogs_row - refunds
    net_profit   = gross_profit - expenses
    margin       = round((net_profit / revenue * 100), 1) if revenue > 0 else 0
    db.close()
    return {
        "period": period,
        "revenue": revenue, "tax_collected": tax_col, "discounts_given": discounts,
        "refunds": refunds, "cogs": cogs_row, "gross_profit": gross_profit,
        "expenses": expenses, "expense_breakdown": [{"category":r[0],"amount":r[1]} for r in exp_cats],
        "net_profit": net_profit, "margin_pct": margin
    }

@router.get("/export/sales")
async def export_sales_csv(month: str = None, current_user=Depends(get_current_user)):
    """Export all sales for a month as CSV."""
    db = get_db()
    period = month or datetime.now().strftime("%Y-%m")
    rows = db.execute("""
        SELECT o.order_number, u.full_name, o.customer_name, o.subtotal, o.tax, o.discount,
               o.total, o.payment_method, o.status, o.created_at
        FROM orders o LEFT JOIN users u ON o.cashier_id=u.id
        WHERE strftime('%Y-%m',o.created_at)=?
        ORDER BY o.created_at
    """, (period,)).fetchall()
    db.close()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Order #","Cashier","Customer","Subtotal","VAT","Discount","Total","Payment","Status","Date"])
    for r in rows:
        w.writerow(list(r))
    return Response(content=buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=sales_{period}.csv"})

@router.get("/export/expenses")
async def export_expenses_csv(month: str = None, current_user=Depends(get_current_user)):
    """Export expenses as CSV."""
    db = get_db()
    period = month or datetime.now().strftime("%Y-%m")
    rows = db.execute("""
        SELECT title, category, amount, date, payment_method, reference, description
        FROM expenses WHERE strftime('%Y-%m',date)=? ORDER BY date
    """, (period,)).fetchall()
    db.close()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Title","Category","Amount","Date","Payment Method","Reference","Description"])
    for r in rows:
        w.writerow(list(r))
    return Response(content=buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=expenses_{period}.csv"})

@router.get("/export/inventory")
async def export_inventory_csv(current_user=Depends(get_current_user)):
    """Export full inventory as CSV."""
    db = get_db()
    rows = db.execute("""
        SELECT p.sku, p.name, p.category, p.price, p.cost, p.stock_quantity, p.reorder_level, s.name
        FROM products p LEFT JOIN suppliers s ON p.supplier_id=s.id
        WHERE p.is_active=1 ORDER BY p.category, p.name
    """).fetchall()
    db.close()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["SKU","Name","Category","Price","Cost","Stock","Reorder Level","Supplier"])
    for r in rows:
        w.writerow(list(r))
    return Response(content=buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=inventory.csv"})
