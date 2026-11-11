"""
Neural POS — Returns & Refunds Module
Full audit trail, stock restoration, refund tracking
"""
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional, List
from models.database import get_db
from routers.auth import get_current_user

router = APIRouter()

class ReturnItem(BaseModel):
    order_item_id: int
    product_id: int
    quantity: int
    reason: Optional[str] = ""

class ReturnCreate(BaseModel):
    order_id: int
    items: List[ReturnItem]
    refund_method: Optional[str] = "cash"
    notes: Optional[str] = ""


@router.get("")
async def list_returns(current_user=Depends(get_current_user)):
    db = get_db()
    rows = db.execute("""
        SELECT r.id, r.return_number, r.order_number, r.refund_amount,
               r.refund_method, r.notes, u.full_name, r.created_at
        FROM returns r LEFT JOIN users u ON r.processed_by=u.id
        ORDER BY r.created_at DESC LIMIT 50
    """).fetchall()
    db.close()
    keys = ["id","return_number","order_number","refund_amount","refund_method","notes","processed_by","created_at"]
    return [dict(zip(keys, r)) for r in rows]

@router.get("/stats")
async def return_stats(current_user=Depends(get_current_user)):
    db = get_db()
    total_count   = db.execute("SELECT COUNT(*) FROM returns").fetchone()[0]
    month_count   = db.execute("SELECT COUNT(*) FROM returns WHERE strftime('%Y-%m',created_at)=strftime('%Y-%m','now')").fetchone()[0]
    month_amount  = db.execute("SELECT COALESCE(SUM(refund_amount),0) FROM returns WHERE strftime('%Y-%m',created_at)=strftime('%Y-%m','now')").fetchone()[0]
    db.close()
    return {"total_returns": total_count, "month_returns": month_count, "month_refund_amount": month_amount}

@router.post("")
async def create_return(data: ReturnCreate, current_user=Depends(get_current_user)):
    db = get_db()
    order = db.execute("SELECT id, order_number FROM orders WHERE id=?", (data.order_id,)).fetchone()
    if not order:
        db.close(); raise HTTPException(404, "Order not found")

    # Calculate refund
    refund_total = 0
    items_detail = []
    for item in data.items:
        prod = db.execute("SELECT name, price FROM products WHERE id=?", (item.product_id,)).fetchone()
        if not prod:
            continue
        refund = item.quantity * prod[1]
        refund_total += refund
        items_detail.append((item.product_id, prod[0], item.quantity, prod[1], refund, item.reason or ""))

    count = db.execute("SELECT COUNT(*) FROM returns").fetchone()[0]
    ret_num = f"RET-{count+1:05d}"

    db.execute("INSERT INTO returns (return_number,order_id,order_number,refund_amount,refund_method,notes,processed_by) VALUES (?,?,?,?,?,?,?)",
               (ret_num, data.order_id, order[1], round(refund_total, 2), data.refund_method, data.notes, current_user["id"]))
    ret_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]

    for pid, pname, qty, uprice, refund, reason in items_detail:
        db.execute("INSERT INTO return_items (return_id,product_id,product_name,quantity,unit_price,refund_amount,reason) VALUES (?,?,?,?,?,?,?)",
                   (ret_id, pid, pname, qty, uprice, refund, reason))
        # Restore stock
        db.execute("UPDATE products SET stock_quantity=stock_quantity+? WHERE id=?", (qty, pid))

    db.commit(); db.close()
    return {"return_number": ret_num, "refund_amount": round(refund_total, 2)}
