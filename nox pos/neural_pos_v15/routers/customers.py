"""
Neural POS — Customer CRM Module
Customer profiles, purchase history, loyalty points, credit/deni tracking
"""
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional
from models.database import get_db
from routers.auth import get_current_user

router = APIRouter()

class CustomerCreate(BaseModel):
    name: str
    phone: Optional[str] = ""
    email: Optional[str] = ""
    address: Optional[str] = ""
    notes: Optional[str] = ""
    credit_limit: Optional[float] = 0.0


COLS = "id,name,phone,email,address,notes,loyalty_points,credit_limit,credit_balance,total_spent,total_orders,is_active,created_at,last_purchase"

def row_to_dict(r):
    return dict(zip(COLS.split(","), r))

@router.get("")
async def list_customers(search: Optional[str] = None, current_user=Depends(get_current_user)):
    db = get_db()
    if search:
        rows = db.execute(f"SELECT {COLS} FROM customers WHERE (name LIKE ? OR phone LIKE ? OR email LIKE ?) AND is_active=1 ORDER BY name",
                          (f"%{search}%", f"%{search}%", f"%{search}%")).fetchall()
    else:
        rows = db.execute(f"SELECT {COLS} FROM customers WHERE is_active=1 ORDER BY total_spent DESC").fetchall()
    db.close()
    return [row_to_dict(r) for r in rows]

@router.get("/stats")
async def customer_stats(current_user=Depends(get_current_user)):
    db = get_db()
    total     = db.execute("SELECT COUNT(*) FROM customers WHERE is_active=1").fetchone()[0]
    vip       = db.execute("SELECT COUNT(*) FROM customers WHERE loyalty_points >= 500 AND is_active=1").fetchone()[0]
    credit_out= db.execute("SELECT COALESCE(SUM(credit_balance),0) FROM customers WHERE credit_balance>0 AND is_active=1").fetchone()[0]
    new_month = db.execute("SELECT COUNT(*) FROM customers WHERE created_at >= date('now','start of month') AND is_active=1").fetchone()[0]
    db.close()
    return {"total": total, "vip": vip, "credit_outstanding": credit_out, "new_this_month": new_month}

@router.get("/{customer_id}")
async def get_customer(customer_id: int, current_user=Depends(get_current_user)):
    db = get_db()
    row = db.execute(f"SELECT {COLS} FROM customers WHERE id=?", (customer_id,)).fetchone()
    if not row:
        db.close(); raise HTTPException(404, "Customer not found")
    c = row_to_dict(row)
    # Recent orders
    orders = db.execute("""
        SELECT order_number, total, payment_method, created_at
        FROM orders WHERE customer_id=? ORDER BY created_at DESC LIMIT 10
    """, (customer_id,)).fetchall()
    c["recent_orders"] = [{"order_number":o[0],"total":o[1],"payment_method":o[2],"created_at":o[3]} for o in orders]
    # Credit history
    credits = db.execute("""
        SELECT type,amount,description,created_at FROM credit_transactions
        WHERE customer_id=? ORDER BY created_at DESC LIMIT 20
    """, (customer_id,)).fetchall()
    c["credit_history"] = [{"type":t[0],"amount":t[1],"description":t[2],"created_at":t[3]} for t in credits]
    db.close()
    return c

@router.post("")
async def create_customer(data: CustomerCreate, current_user=Depends(get_current_user)):
    db = get_db()
    db.execute("INSERT INTO customers (name,phone,email,address,notes,credit_limit) VALUES (?,?,?,?,?,?)",
               (data.name, data.phone, data.email, data.address, data.notes, data.credit_limit))
    cid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.commit()
    row = db.execute(f"SELECT {COLS} FROM customers WHERE id=?", (cid,)).fetchone()
    db.close()
    return row_to_dict(row)

@router.put("/{customer_id}")
async def update_customer(customer_id: int, data: CustomerCreate, current_user=Depends(get_current_user)):
    db = get_db()
    if not db.execute("SELECT id FROM customers WHERE id=?", (customer_id,)).fetchone():
        db.close(); raise HTTPException(404, "Customer not found")
    db.execute("UPDATE customers SET name=?,phone=?,email=?,address=?,notes=?,credit_limit=? WHERE id=?",
               (data.name, data.phone, data.email, data.address, data.notes, data.credit_limit, customer_id))
    db.commit()
    row = db.execute(f"SELECT {COLS} FROM customers WHERE id=?", (customer_id,)).fetchone()
    db.close()
    return row_to_dict(row)

@router.delete("/{customer_id}")
async def delete_customer(customer_id: int, current_user=Depends(get_current_user)):
    db = get_db()
    db.execute("UPDATE customers SET is_active=0 WHERE id=?", (customer_id,))
    db.commit(); db.close()
    return {"deleted": True}

@router.post("/{customer_id}/credit")
async def add_credit_transaction(customer_id: int, body: dict, current_user=Depends(get_current_user)):
    db = get_db()
    cust = db.execute("SELECT id,credit_balance,credit_limit FROM customers WHERE id=?", (customer_id,)).fetchone()
    if not cust:
        db.close(); raise HTTPException(404, "Customer not found")
    amount = float(body.get("amount", 0))
    txn_type = body.get("type", "charge")  # charge | payment
    desc = body.get("description", "")
    new_balance = cust[1] + amount if txn_type == "charge" else cust[1] - amount
    new_balance = max(0, new_balance)
    db.execute("UPDATE customers SET credit_balance=? WHERE id=?", (new_balance, customer_id))
    db.execute("INSERT INTO credit_transactions (customer_id,type,amount,description,created_by) VALUES (?,?,?,?,?)",
               (customer_id, txn_type, amount, desc, current_user["id"]))
    db.commit(); db.close()
    return {"new_balance": new_balance, "type": txn_type}

@router.post("/{customer_id}/loyalty")
async def adjust_loyalty(customer_id: int, body: dict, current_user=Depends(get_current_user)):
    db = get_db()
    points = int(body.get("points", 0))
    db.execute("UPDATE customers SET loyalty_points = loyalty_points + ? WHERE id=?", (points, customer_id))
    db.commit()
    row = db.execute("SELECT loyalty_points FROM customers WHERE id=?", (customer_id,)).fetchone()
    db.close()
    return {"loyalty_points": row[0] if row else 0}
