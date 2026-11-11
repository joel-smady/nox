"""
Neural POS — Suppliers Module (v2)
Full supplier management: profiles, purchase orders with line items,
stock receiving, supplier performance, reorder suggestions
"""
from fastapi import APIRouter, HTTPException, Request, Depends
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime, date, timedelta
from models.database import get_db
from routers.auth import get_current_user
import random, string

router = APIRouter()

# ── Models ────────────────────────────────────────────────────────────────────

class SupplierCreate(BaseModel):
    name: str
    contact_person: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    country: Optional[str] = "Kenya"
    payment_terms: Optional[str] = "Net 30"
    lead_time_days: int = 7
    notes: Optional[str] = ""
    bank_account: Optional[str] = ""
    mpesa_till: Optional[str] = ""

class POItem(BaseModel):
    product_id: int
    quantity: int
    unit_cost: float
    notes: Optional[str] = ""

class PurchaseOrderCreate(BaseModel):
    supplier_id: int
    items: List[POItem]
    expected_date: Optional[str] = None
    notes: Optional[str] = None
    shipping_cost: Optional[float] = 0

class ReceiveItem(BaseModel):
    po_item_id: int
    product_id: int
    quantity_received: int

class ReceivePO(BaseModel):
    items: List[ReceiveItem]
    notes: Optional[str] = ""

# ── Table setup ───────────────────────────────────────────────────────────────


def gen_po_number(db):
    n = db.execute("SELECT COUNT(*) FROM purchase_orders").fetchone()[0] + 1
    return f"PO-{n:05d}"

# ── Supplier routes ───────────────────────────────────────────────────────────

@router.get("/")
async def list_suppliers(request: Request):
    user = get_current_user(request)
    if not user: raise HTTPException(401, "Authentication required")
    db = get_db()
    rows = db.execute("""
        SELECT s.*, COUNT(DISTINCT p.id) as product_count,
               COUNT(DISTINCT po.id) as po_count,
               COALESCE(SUM(po.total_amount),0) as total_spent
        FROM suppliers s
        LEFT JOIN products p  ON s.id = p.supplier_id AND p.is_active = 1
        LEFT JOIN purchase_orders po ON s.id = po.supplier_id
        WHERE s.is_active = 1
        GROUP BY s.id ORDER BY s.name
    """).fetchall()
    db.close()
    return [dict(r) for r in rows]

@router.get("/stats")
async def supplier_stats(current_user=Depends(get_current_user)):
    db = get_db()
    total    = db.execute("SELECT COUNT(*) FROM suppliers WHERE is_active=1").fetchone()[0]
    po_open  = db.execute("SELECT COUNT(*) FROM purchase_orders WHERE status IN ('pending','ordered')").fetchone()[0]
    po_month = db.execute("SELECT COALESCE(SUM(total_amount),0) FROM purchase_orders WHERE strftime('%Y-%m',created_at)=strftime('%Y-%m','now')").fetchone()[0]
    overdue  = db.execute("SELECT COUNT(*) FROM purchase_orders WHERE status='pending' AND expected_date < date('now')").fetchone()[0]
    db.close()
    return {"total_suppliers": total, "open_pos": po_open, "month_spend": po_month, "overdue_pos": overdue}

@router.get("/{supplier_id}")
async def get_supplier(supplier_id: int, current_user=Depends(get_current_user)):
    db = get_db()
    row = db.execute("SELECT * FROM suppliers WHERE id=?", (supplier_id,)).fetchone()
    if not row: db.close(); raise HTTPException(404, "Supplier not found")
    s = dict(row)
    s["products"] = [dict(r) for r in db.execute(
        "SELECT id,sku,name,price,cost,stock_quantity,reorder_level FROM products WHERE supplier_id=? AND is_active=1 ORDER BY name",
        (supplier_id,)).fetchall()]
    s["recent_pos"] = [dict(r) for r in db.execute(
        "SELECT * FROM purchase_orders WHERE supplier_id=? ORDER BY created_at DESC LIMIT 5",
        (supplier_id,)).fetchall()]
    db.close()
    return s

@router.post("/")
async def create_supplier(supplier: SupplierCreate, request: Request):
    user = get_current_user(request)
    if not user or user["role"] not in ["admin","manager"]:
        raise HTTPException(403, "Manager or Admin required")
    db = get_db()
    db.execute("""
        INSERT INTO suppliers
        (name,contact_person,email,phone,address,city,country,payment_terms,lead_time_days,notes,bank_account,mpesa_till)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
    """, (supplier.name, supplier.contact_person, supplier.email, supplier.phone,
          supplier.address, supplier.city, supplier.country or "Kenya",
          supplier.payment_terms, supplier.lead_time_days,
          supplier.notes or "", supplier.bank_account or "", supplier.mpesa_till or ""))
    sid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.commit(); db.close()
    return {"success": True, "supplier_id": sid}

@router.put("/{supplier_id}")
async def update_supplier(supplier_id: int, supplier: SupplierCreate, request: Request):
    user = get_current_user(request)
    if not user or user["role"] not in ["admin","manager"]:
        raise HTTPException(403, "Manager or Admin required")
    db = get_db()
    db.execute("""
        UPDATE suppliers SET name=?,contact_person=?,email=?,phone=?,address=?,city=?,country=?,
        payment_terms=?,lead_time_days=?,notes=?,bank_account=?,mpesa_till=?
        WHERE id=?
    """, (supplier.name, supplier.contact_person, supplier.email, supplier.phone,
          supplier.address, supplier.city, supplier.country or "Kenya",
          supplier.payment_terms, supplier.lead_time_days,
          supplier.notes or "", supplier.bank_account or "", supplier.mpesa_till or "",
          supplier_id))
    db.commit(); db.close()
    return {"success": True}

@router.delete("/{supplier_id}")
async def delete_supplier(supplier_id: int, request: Request):
    user = get_current_user(request)
    if not user or user["role"] != "admin":
        raise HTTPException(403, "Admin required")
    db = get_db()
    db.execute("UPDATE suppliers SET is_active=0 WHERE id=?", (supplier_id,))
    db.commit(); db.close()
    return {"success": True}

@router.get("/{supplier_id}/products")
async def supplier_products(supplier_id: int, request: Request):
    user = get_current_user(request)
    if not user: raise HTTPException(401, "Authentication required")
    db = get_db()
    rows = db.execute(
        "SELECT * FROM products WHERE supplier_id=? AND is_active=1 ORDER BY name",
        (supplier_id,)).fetchall()
    db.close()
    return [dict(r) for r in rows]

@router.get("/reorder-suggestions")
async def reorder_suggestions(current_user=Depends(get_current_user)):
    """Products at or below reorder level, grouped by supplier."""
    db = get_db()
    rows = db.execute("""
        SELECT p.id, p.sku, p.name, p.stock_quantity, p.reorder_level, p.cost,
               s.id as supplier_id, s.name as supplier_name, s.lead_time_days
        FROM products p
        LEFT JOIN suppliers s ON p.supplier_id = s.id
        WHERE p.is_active=1 AND p.stock_quantity <= p.reorder_level
        ORDER BY s.name, p.name
    """).fetchall()
    db.close()
    # Suggest order qty = 2x reorder level - current stock
    result = []
    for r in rows:
        suggest_qty = max(1, (r[3] * 2) - r[2]) if r[3] else r[4] * 2
        result.append({
            "product_id": r[0], "sku": r[1], "name": r[2],
            "stock": r[3], "reorder_level": r[4], "cost": r[5],
            "supplier_id": r[6], "supplier_name": r[7] or "Unassigned",
            "lead_time_days": r[8] or 7,
            "suggested_qty": suggest_qty,
            "estimated_cost": round(suggest_qty * (r[5] or 0), 2)
        })
    return result

# ── Purchase Order routes ─────────────────────────────────────────────────────

@router.get("/purchase-orders/all")
async def list_purchase_orders(request: Request, status: Optional[str] = None):
    user = get_current_user(request)
    if not user: raise HTTPException(401, "Authentication required")
    db = get_db()
    q = """
        SELECT po.*, s.name as supplier_name, u.full_name as created_by_name
        FROM purchase_orders po
        LEFT JOIN suppliers s ON po.supplier_id=s.id
        LEFT JOIN users u ON po.created_by=u.id
        WHERE 1=1
    """
    params = []
    if status: q += " AND po.status=?"; params.append(status)
    q += " ORDER BY po.created_at DESC"
    pos = db.execute(q, params).fetchall()

    result = []
    for p in pos:
        po = dict(p)
        po["items"] = [dict(r) for r in db.execute(
            "SELECT * FROM po_items WHERE po_id=?", (p["id"],)).fetchall()]
        result.append(po)
    db.close()
    return result

@router.get("/purchase-orders/{po_id}")
async def get_purchase_order(po_id: int, current_user=Depends(get_current_user)):
    db = get_db()
    po = db.execute("""
        SELECT po.*, s.name as supplier_name, s.email as supplier_email,
               s.phone as supplier_phone, u.full_name as created_by_name
        FROM purchase_orders po
        LEFT JOIN suppliers s ON po.supplier_id=s.id
        LEFT JOIN users u ON po.created_by=u.id
        WHERE po.id=?
    """, (po_id,)).fetchone()
    if not po: db.close(); raise HTTPException(404, "PO not found")
    result = dict(po)
    result["items"] = [dict(r) for r in db.execute(
        """SELECT pi.*, p.name as product_name, p.sku
           FROM po_items pi LEFT JOIN products p ON pi.product_id=p.id
           WHERE pi.po_id=?""", (po_id,)).fetchall()]
    result["receive_log"] = [dict(r) for r in db.execute(
        "SELECT * FROM po_receive_log WHERE po_id=? ORDER BY received_at DESC", (po_id,)).fetchall()]
    db.close()
    return result

@router.post("/purchase-orders")
async def create_purchase_order(po: PurchaseOrderCreate, request: Request):
    user = get_current_user(request)
    if not user or user["role"] not in ["admin","manager"]:
        raise HTTPException(403, "Manager or Admin required")

    db = get_db()
    items_detail = []
    subtotal = 0.0

    for item in po.items:
        prod = db.execute("SELECT name,sku,cost FROM products WHERE id=?", (item.product_id,)).fetchone()
        if not prod: continue
        line_cost = round(item.unit_cost * item.quantity, 2)
        subtotal += line_cost
        items_detail.append({
            "product_id": item.product_id,
            "product_name": prod["name"],
            "quantity_ordered": item.quantity,
            "unit_cost": item.unit_cost,
            "total_cost": line_cost,
            "notes": item.notes or ""
        })

    total = round(subtotal + (po.shipping_cost or 0), 2)
    po_num = gen_po_number(db)
    uid = user.get("user_id") or user.get("id")

    db.execute("""
        INSERT INTO purchase_orders
        (po_number, supplier_id, total_amount, status, expected_date, notes, created_by)
        VALUES (?,?,?,'pending',?,?,?)
    """, (po_num, po.supplier_id, total, po.expected_date, po.notes or "", uid))
    po_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]

    for it in items_detail:
        db.execute("""
            INSERT INTO po_items
            (po_id,product_id,product_name,quantity_ordered,unit_cost,total_cost,notes)
            VALUES (?,?,?,?,?,?,?)
        """, (po_id, it["product_id"], it["product_name"], it["quantity_ordered"],
              it["unit_cost"], it["total_cost"], it["notes"]))

    db.commit(); db.close()
    return {"success": True, "po_number": po_num, "po_id": po_id, "total": total}

@router.post("/purchase-orders/{po_id}/receive")
async def receive_purchase_order(po_id: int, data: ReceivePO, request: Request):
    """Mark goods as received — updates stock and PO status."""
    user = get_current_user(request)
    if not user or user["role"] not in ["admin","manager"]:
        raise HTTPException(403, "Manager or Admin required")

    db = get_db()
    po = db.execute("SELECT id, status FROM purchase_orders WHERE id=?", (po_id,)).fetchone()
    if not po: db.close(); raise HTTPException(404, "PO not found")
    if po["status"] == "completed":
        db.close(); raise HTTPException(400, "PO already fully received")

    uid = user.get("user_id") or user.get("id")
    now = datetime.now().isoformat()

    for item in data.items:
        if item.quantity_received <= 0: continue
        # Update po_item received qty
        db.execute("""
            UPDATE po_items SET quantity_received = quantity_received + ?
            WHERE id=? AND po_id=?
        """, (item.quantity_received, item.po_item_id, po_id))
        # Add to product stock
        prod = db.execute("SELECT name FROM products WHERE id=?", (item.product_id,)).fetchone()
        prod_name = prod["name"] if prod else "Unknown"
        db.execute("""
            UPDATE products SET stock_quantity = stock_quantity + ?, updated_at=?
            WHERE id=?
        """, (item.quantity_received, now, item.product_id))
        # Log the receipt
        db.execute("""
            INSERT INTO po_receive_log
            (po_id, product_id, product_name, quantity_received, received_by, notes)
            VALUES (?,?,?,?,?,?)
        """, (po_id, item.product_id, prod_name, item.quantity_received, uid, data.notes or ""))

    # Check if all items fully received → mark completed, else partial
    all_items = db.execute(
        "SELECT SUM(quantity_ordered) as ordered, SUM(quantity_received) as received FROM po_items WHERE po_id=?",
        (po_id,)).fetchone()
    new_status = "completed" if (all_items[1] or 0) >= (all_items[0] or 0) else "partial"
    db.execute("UPDATE purchase_orders SET status=?, received_date=? WHERE id=?",
               (new_status, date.today().isoformat(), po_id))
    db.commit(); db.close()

    return {"success": True, "status": new_status,
            "message": f"Stock updated. PO status: {new_status.upper()}"}

@router.put("/purchase-orders/{po_id}/status")
async def update_po_status(po_id: int, body: dict, current_user=Depends(get_current_user)):
    if current_user.get("role") not in ["admin","manager"]:
        raise HTTPException(403, "Manager or Admin required")
    db = get_db()
    status = body.get("status", "pending")
    db.execute("UPDATE purchase_orders SET status=? WHERE id=?", (status, po_id))
    db.commit(); db.close()
    return {"updated": True}

@router.delete("/purchase-orders/{po_id}")
async def delete_purchase_order(po_id: int, current_user=Depends(get_current_user)):
    if current_user.get("role") not in ["admin","manager"]:
        raise HTTPException(403, "Manager or Admin required")
    db = get_db()
    db.execute("DELETE FROM po_items WHERE po_id=?", (po_id,))
    db.execute("DELETE FROM purchase_orders WHERE id=?", (po_id,))
    db.commit(); db.close()
    return {"deleted": True}
