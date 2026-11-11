"""
Neural POS — Credit Sales (Deni) Module
Sell on credit, track balances, record payments, auto-alert overdue
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional, List
from models.database import get_db
from routers.auth import get_current_user
import datetime

router = APIRouter()

class CreditSaleCreate(BaseModel):
    customer_id: int
    items: list           # same format as pos sale items
    discount: float = 0
    due_date: Optional[str] = None
    notes: Optional[str] = ""

class CreditPayment(BaseModel):
    credit_sale_id: int
    amount: float
    payment_method: str = "cash"
    notes: Optional[str] = ""


def next_sale_number(db):
    n = db.execute("SELECT COUNT(*) FROM credit_sales").fetchone()[0] + 1
    return f"CR-{n:05d}"

@router.get("")
async def list_credit_sales(status: Optional[str] = None,
                             customer_id: Optional[int] = None,
                             current_user=Depends(get_current_user)):
    db = get_db()
    # Auto-mark overdue
    today = datetime.date.today().isoformat()
    db.execute("""
        UPDATE credit_sales SET status='overdue', updated_at=datetime('now')
        WHERE due_date < ? AND status='unpaid'
    """, (today,))
    db.commit()

    q = """SELECT cs.id, cs.sale_number, cs.customer_name, cs.total_amount,
                  cs.amount_paid, cs.balance, cs.due_date, cs.status, cs.created_at,
                  c.phone as customer_phone
           FROM credit_sales cs
           LEFT JOIN customers c ON cs.customer_id=c.id WHERE 1=1"""
    params = []
    if status: q += " AND cs.status=?"; params.append(status)
    if customer_id: q += " AND cs.customer_id=?"; params.append(customer_id)
    q += " ORDER BY cs.created_at DESC"
    rows = db.execute(q, params).fetchall()
    db.close()
    keys = ["id","sale_number","customer_name","total_amount","amount_paid","balance",
            "due_date","status","created_at","customer_phone"]
    return [dict(zip(keys, r)) for r in rows]

@router.get("/stats")
async def credit_stats(current_user=Depends(get_current_user)):
    db = get_db()
    total_out  = db.execute("SELECT COALESCE(SUM(balance),0) FROM credit_sales WHERE status IN ('unpaid','overdue')").fetchone()[0]
    overdue_n  = db.execute("SELECT COUNT(*) FROM credit_sales WHERE status='overdue'").fetchone()[0]
    total_n    = db.execute("SELECT COUNT(*) FROM credit_sales").fetchone()[0]
    collected  = db.execute("SELECT COALESCE(SUM(amount),0) FROM credit_payments WHERE date(created_at)>=date('now','start of month')").fetchone()[0]
    db.close()
    return {"total_outstanding": total_out, "overdue_count": overdue_n,
            "total_credit_sales": total_n, "collected_this_month": collected}

@router.get("/{sale_id}")
async def get_credit_sale(sale_id: int, current_user=Depends(get_current_user)):
    db = get_db()
    row = db.execute("""
        SELECT cs.*, c.phone, c.email
        FROM credit_sales cs LEFT JOIN customers c ON cs.customer_id=c.id WHERE cs.id=?
    """, (sale_id,)).fetchone()
    if not row: db.close(); raise HTTPException(404, "Credit sale not found")
    result = dict(row)
    result["items"] = [dict(r) for r in db.execute(
        "SELECT * FROM credit_sale_items WHERE credit_sale_id=?", (sale_id,)).fetchall()]
    result["payments"] = [dict(r) for r in db.execute(
        "SELECT * FROM credit_payments WHERE credit_sale_id=? ORDER BY created_at DESC", (sale_id,)).fetchall()]
    db.close(); return result

@router.post("")
async def create_credit_sale(data: CreditSaleCreate, current_user=Depends(get_current_user)):
    db = get_db()
    cust = db.execute("SELECT name, credit_limit, credit_balance FROM customers WHERE id=? AND is_active=1",
                      (data.customer_id,)).fetchone()
    if not cust: db.close(); raise HTTPException(404, "Customer not found")

    # Calculate total
    subtotal = 0.0
    items_detail = []
    for item in data.items:
        prod = db.execute("SELECT name, price FROM products WHERE id=? AND is_active=1",
                          (item.get("product_id"),)).fetchone()
        if not prod: continue
        qty = item.get("quantity", 1)
        line = prod["price"] * qty
        subtotal += line
        items_detail.append({"product_id": item["product_id"], "product_name": prod["name"],
                              "quantity": qty, "unit_price": prod["price"], "total_price": line})
        # Deduct stock immediately
        db.execute("UPDATE products SET stock_quantity=MAX(0,stock_quantity-?) WHERE id=?",
                   (qty, item["product_id"]))

    tax = round(subtotal * 0.16, 2)
    discount = min(data.discount or 0, subtotal)
    total = round(subtotal + tax - discount, 2)

    # Check credit limit
    new_balance = cust["credit_balance"] + total
    if cust["credit_limit"] > 0 and new_balance > cust["credit_limit"]:
        db.close()
        raise HTTPException(400, f"Exceeds credit limit. Available: KES {cust['credit_limit'] - cust['credit_balance']:,.2f}")

    sale_num = next_sale_number(db)
    due_date = data.due_date or (datetime.date.today() + datetime.timedelta(days=30)).isoformat()
    uid = current_user.get("user_id") or current_user.get("id")

    db.execute("""
        INSERT INTO credit_sales
        (sale_number,customer_id,customer_name,subtotal,tax,discount,total_amount,balance,due_date,notes,cashier_id)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """, (sale_num, data.customer_id, cust["name"], subtotal, tax, discount,
          total, total, due_date, data.notes or "", uid))
    cid = db.execute("SELECT last_insert_rowid()").fetchone()[0]

    for it in items_detail:
        db.execute("INSERT INTO credit_sale_items (credit_sale_id,product_id,product_name,quantity,unit_price,total_price) VALUES (?,?,?,?,?,?)",
                   (cid, it["product_id"], it["product_name"], it["quantity"], it["unit_price"], it["total_price"]))

    # Update customer credit balance
    db.execute("UPDATE customers SET credit_balance=credit_balance+? WHERE id=?", (total, data.customer_id))
    db.commit(); db.close()
    return {"success": True, "sale_number": sale_num, "total": total, "due_date": due_date}

@router.post("/payment")
async def record_payment(data: CreditPayment, current_user=Depends(get_current_user)):
    db = get_db()
    sale = db.execute("SELECT customer_id, balance, total_amount FROM credit_sales WHERE id=?",
                      (data.credit_sale_id,)).fetchone()
    if not sale: db.close(); raise HTTPException(404, "Credit sale not found")
    if data.amount <= 0: db.close(); raise HTTPException(400, "Amount must be positive")

    new_balance = max(0, round(sale["balance"] - data.amount, 2))
    status = "paid" if new_balance <= 0 else "partial"
    uid = current_user.get("user_id") or current_user.get("id")

    db.execute("""
        UPDATE credit_sales SET amount_paid=amount_paid+?, balance=?, status=?, updated_at=datetime('now')
        WHERE id=?
    """, (data.amount, new_balance, status, data.credit_sale_id))
    db.execute("INSERT INTO credit_payments (credit_sale_id,amount,payment_method,notes,recorded_by) VALUES (?,?,?,?,?)",
               (data.credit_sale_id, data.amount, data.payment_method, data.notes or "", uid))
    # Reduce customer credit balance
    db.execute("UPDATE customers SET credit_balance=MAX(0,credit_balance-?) WHERE id=?",
               (data.amount, sale["customer_id"]))
    db.commit(); db.close()
    return {"success": True, "new_balance": new_balance, "status": status}
