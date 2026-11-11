"""
Neural POS — Analytics Router
Sales analytics, reports, trends
"""

from fastapi import APIRouter, HTTPException, Request
from datetime import datetime, timedelta
from models.database import get_db
from routers.auth import get_current_user

router = APIRouter()

@router.get("/summary")
async def get_summary(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")

    conn = get_db()
    cursor = conn.cursor()

    today = datetime.now().date().isoformat()
    week_start = (datetime.now() - timedelta(days=7)).date().isoformat()
    month_start = (datetime.now().replace(day=1)).date().isoformat()

    cursor.execute("SELECT COUNT(*), COALESCE(SUM(total), 0) FROM orders WHERE DATE(created_at) = ?", (today,))
    t = cursor.fetchone()

    cursor.execute("SELECT COUNT(*), COALESCE(SUM(total), 0) FROM orders WHERE DATE(created_at) >= ?", (week_start,))
    w = cursor.fetchone()

    cursor.execute("SELECT COUNT(*), COALESCE(SUM(total), 0) FROM orders WHERE DATE(created_at) >= ?", (month_start,))
    m = cursor.fetchone()

    cursor.execute("SELECT COUNT(*) FROM products WHERE is_active = 1")
    prod_count = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM products WHERE stock_quantity <= reorder_level AND is_active = 1")
    low_stock = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM suppliers WHERE is_active = 1")
    supp_count = cursor.fetchone()[0]

    conn.close()
    return {
        "today": {"orders": t[0], "revenue": round(t[1], 2)},
        "week": {"orders": w[0], "revenue": round(w[1], 2)},
        "month": {"orders": m[0], "revenue": round(m[1], 2)},
        "inventory": {"total_products": prod_count, "low_stock": low_stock},
        "suppliers": supp_count
    }

@router.get("/sales-trend")
async def sales_trend(request: Request, days: int = 30):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")

    conn = get_db()
    cursor = conn.cursor()
    start = (datetime.now() - timedelta(days=days)).date().isoformat()
    cursor.execute("""
        SELECT DATE(created_at) as date, COUNT(*) as orders, COALESCE(SUM(total), 0) as revenue
        FROM orders
        WHERE DATE(created_at) >= ?
        GROUP BY DATE(created_at)
        ORDER BY date ASC
    """, (start,))
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()

    # Fill missing dates
    result = []
    current = datetime.now() - timedelta(days=days)
    existing = {r["date"]: r for r in rows}
    for i in range(days + 1):
        d = (current + timedelta(days=i)).date().isoformat()
        if d in existing:
            result.append(existing[d])
        else:
            result.append({"date": d, "orders": 0, "revenue": 0})
    return result

@router.get("/top-products")
async def top_products(request: Request, limit: int = 10):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT oi.product_name, SUM(oi.quantity) as total_sold, SUM(oi.total_price) as revenue
        FROM order_items oi
        JOIN orders o ON oi.order_id = o.id
        GROUP BY oi.product_name
        ORDER BY total_sold DESC
        LIMIT ?
    """, (limit,))
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows

@router.get("/payment-methods")
async def payment_methods(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT payment_method, COUNT(*) as count, SUM(total) as total
        FROM orders GROUP BY payment_method
    """)
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows

@router.get("/hourly-sales")
async def hourly_sales(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT strftime('%H', created_at) as hour,
               COUNT(*) as orders,
               COALESCE(SUM(total), 0) as revenue
        FROM orders
        WHERE DATE(created_at) >= DATE('now', '-7 days')
        GROUP BY hour
        ORDER BY hour
    """)
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    all_hours = {str(h).zfill(2): {"hour": str(h).zfill(2), "orders": 0, "revenue": 0} for h in range(24)}
    for r in rows:
        all_hours[r["hour"]] = r
    return list(all_hours.values())

@router.get("/category-revenue")
async def category_revenue(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT p.category, SUM(oi.total_price) as revenue, SUM(oi.quantity) as units
        FROM order_items oi
        JOIN products p ON oi.product_id = p.id
        GROUP BY p.category
        ORDER BY revenue DESC
    """)
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows

@router.get("/cashier-performance")
async def cashier_performance(request: Request):
    user = get_current_user(request)
    if not user or user["role"] not in ["admin", "manager"]:
        raise HTTPException(status_code=403, detail="Manager or Admin access required")
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT u.full_name, u.username, COUNT(o.id) as orders, COALESCE(SUM(o.total), 0) as revenue
        FROM users u
        LEFT JOIN orders o ON u.id = o.cashier_id
        WHERE u.role = 'cashier'
        GROUP BY u.id
        ORDER BY revenue DESC
    """)
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows
