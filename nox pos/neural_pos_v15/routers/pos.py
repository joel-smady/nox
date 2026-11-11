"""
Neural POS — Point of Sale Router
Handles sales transactions, cart management, receipts
"""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from typing import List, Optional, Dict
from datetime import datetime
from models.database import get_db
from routers.auth import get_current_user
import random
import string

def _model_to_dict(model) -> dict:
    """Pydantic v1/v2 compatible model serialization."""
    if hasattr(model, 'model_dump'):
        return model.model_dump()
    return model.dict()


router = APIRouter()

class CartItem(BaseModel):
    product_id: int
    quantity: int
    variant_id: Optional[int] = None

class SplitAmounts(BaseModel):
    cash:  float = 0
    mpesa: float = 0
    card:  float = 0

class SaleRequest(BaseModel):
    items: List[CartItem]
    customer_name: Optional[str] = None
    customer_email: Optional[str] = None
    payment_method: str = "cash"
    discount: float = 0.0
    notes: Optional[str] = None
    split_amounts: Optional[SplitAmounts] = None

def generate_order_number():
    """Timestamp + random suffix — extremely low collision probability."""
    import time
    ts = int(time.time() * 1000) % 1000000  # last 6 digits of ms timestamp
    rand = ''.join(random.choices(string.digits, k=3))
    return f"ORD-{ts:06d}{rand}"

@router.get("/products")
async def get_pos_products(request: Request, search: str = ""):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    conn = get_db()
    cursor = conn.cursor()
    if search:
        cursor.execute("""
            SELECT id, sku, name, description, category, price, stock_quantity, barcode
            FROM products
            WHERE is_active = 1 AND stock_quantity > 0
            AND (name LIKE ? OR sku LIKE ? OR category LIKE ? OR barcode LIKE ?)
            ORDER BY name
        """, (f"%{search}%", f"%{search}%", f"%{search}%", f"%{search}%"))
    else:
        cursor.execute("""
            SELECT id, sku, name, description, category, price, stock_quantity, barcode
            FROM products WHERE is_active = 1 AND stock_quantity > 0
            ORDER BY category, name
        """)
    products = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return products

@router.post("/sale")
async def process_sale(sale: SaleRequest, request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")

    conn = get_db()
    cursor = conn.cursor()
    # Atomic transaction — all-or-nothing: order insert + stock deduction
    cursor.execute("BEGIN IMMEDIATE")

    subtotal = 0.0
    items_detail = []

    try:
        for item in sale.items:
            # Handle variant pricing if variant_id provided
            effective_price = None
            if item.variant_id:
                vrow = cursor.execute(
                    "SELECT price_adjustment FROM product_variants WHERE id=? AND is_active=1",
                    (item.variant_id,)
                ).fetchone()
                if vrow:
                    prod = cursor.execute("SELECT price FROM products WHERE id=?", (item.product_id,)).fetchone()
                    if prod:
                        effective_price = prod["price"] + (vrow["price_adjustment"] or 0)

            cursor.execute("SELECT * FROM products WHERE id = ? AND is_active = 1", (item.product_id,))
            product = cursor.fetchone()
            if not product:
                raise HTTPException(status_code=404, detail=f"Product {item.product_id} not found")

            # Check stock — variant or base product
            if item.variant_id:
                vstk = cursor.execute(
                    "SELECT stock_quantity, name FROM product_variants WHERE id=?", (item.variant_id,)
                ).fetchone()
                if vstk and vstk["stock_quantity"] < item.quantity:
                    raise HTTPException(status_code=400, detail="Insufficient stock for variant")
            elif product["stock_quantity"] < item.quantity:
                raise HTTPException(status_code=400, detail=f"Insufficient stock for {product['name']}")

            unit_price = effective_price if effective_price is not None else product["price"]
            item_total = unit_price * item.quantity
            subtotal += item_total
            items_detail.append({
                "product_id": product["id"],
                "product_name": product["name"],
                "quantity": item.quantity,
                "unit_price": unit_price,
                "total_price": item_total,
                "variant_id": item.variant_id
            })

        tax = round(subtotal * 0.16, 2)
        discount = min(sale.discount, subtotal)
        total = round(subtotal + tax - discount, 2)
        order_num = generate_order_number()

        # Build notes with split amounts if applicable
        notes = sale.notes or ""
        if sale.split_amounts:
            s = sale.split_amounts
            notes = f"Split: Cash KES{s.cash:.0f} | M-Pesa KES{s.mpesa:.0f} | Card KES{s.card:.0f}"

        cursor.execute("""
            INSERT INTO orders (order_number, cashier_id, customer_name, customer_email,
                               subtotal, tax, discount, total, payment_method, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (order_num, user.get("user_id") or user.get("id"),
              sale.customer_name, sale.customer_email,
              subtotal, tax, discount, total, sale.payment_method, notes))

        order_id = cursor.lastrowid

        for item in items_detail:
            cursor.execute("""
                INSERT INTO order_items (order_id, product_id, product_name, quantity, unit_price, total_price)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (order_id, item["product_id"], item["product_name"],
                  item["quantity"], item["unit_price"], item["total_price"]))

            # Deduct from variant stock if applicable
            if item["variant_id"]:
                cursor.execute(
                    "UPDATE product_variants SET stock_quantity = MAX(0, stock_quantity - ?) WHERE id=?",
                    (item["quantity"], item["variant_id"])
                )
            else:
                cursor.execute("""
                    UPDATE products SET stock_quantity = MAX(0, stock_quantity - ?), updated_at = ?
                    WHERE id = ?
                """, (item["quantity"], datetime.now().isoformat(), item["product_id"]))

        conn.commit()

    except HTTPException:
        try: conn.rollback()
        except: pass
        conn.close()
        raise
    except Exception as _sale_err:
        try: conn.rollback()
        except: pass
        conn.close()
        raise HTTPException(status_code=500, detail=f"Sale failed: {_sale_err}")

    conn.close()

    # Access log
    try:
        from routers.access_log import log_action
        log_action(request, "sale",
                   f"{order_num} · KES {total:,.2f} · {sale.payment_method} · {len(items_detail)} items",
                   user=user)
    except Exception:
        pass

    # Auto-print receipt if printer is configured
    printer_printed = False
    printer_error   = None
    try:
        from routers.hardware import build_and_print_receipt, get_hw_config
        hw_cfg = get_hw_config()
        if hw_cfg.get("connection_type"):
            order_for_print = {
                "id": order_id,
                "order_number": order_num,
                "created_at": datetime.now().isoformat(),
                "cashier_name": user.get("full_name") or user.get("username"),
                "customer_name": sale.customer_name,
                "subtotal": subtotal,
                "tax": tax,
                "discount": discount,
                "total": total,
                "payment_method": sale.payment_method,
                "notes": notes,
            }
            build_and_print_receipt(order_for_print, items_detail, hw_cfg)
            printer_printed = True
    except Exception as _pe:
        printer_error = str(_pe)   # don't fail the sale if printer is offline

    return {
        "success": True,
        "order_number": order_num,
        "order_id": order_id,
        "subtotal": subtotal,
        "tax": tax,
        "discount": discount,
        "total": total,
        "items": items_detail,
        "payment_method": sale.payment_method,
        "split_amounts": _model_to_dict(sale.split_amounts) if sale.split_amounts else None,
        "printer_printed": printer_printed,
        "printer_error": printer_error,
    }

@router.get("/orders")
async def get_orders(request: Request, limit: int = 50, search: str = ""):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    conn = get_db()
    cursor = conn.cursor()

    uid = user.get("user_id") or user.get("id")
    if user["role"] == "cashier":
        if search:
            cursor.execute("""
                SELECT o.*, u.full_name as cashier_name
                FROM orders o LEFT JOIN users u ON o.cashier_id = u.id
                WHERE o.cashier_id = ? AND (o.order_number LIKE ? OR o.customer_name LIKE ?)
                ORDER BY o.created_at DESC LIMIT ?
            """, (uid, f"%{search}%", f"%{search}%", limit))
        else:
            cursor.execute("""
                SELECT o.*, u.full_name as cashier_name
                FROM orders o LEFT JOIN users u ON o.cashier_id = u.id
                WHERE o.cashier_id = ?
                ORDER BY o.created_at DESC LIMIT ?
            """, (uid, limit))
    else:
        if search:
            cursor.execute("""
                SELECT o.*, u.full_name as cashier_name
                FROM orders o LEFT JOIN users u ON o.cashier_id = u.id
                WHERE o.order_number LIKE ? OR o.customer_name LIKE ? OR o.payment_method LIKE ?
                ORDER BY o.created_at DESC LIMIT ?
            """, (f"%{search}%", f"%{search}%", f"%{search}%", limit))
        else:
            cursor.execute("""
                SELECT o.*, u.full_name as cashier_name
                FROM orders o LEFT JOIN users u ON o.cashier_id = u.id
                ORDER BY o.created_at DESC LIMIT ?
            """, (limit,))

    orders = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return orders

@router.get("/orders/{order_id}")
async def get_order_detail(order_id: int, request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM orders WHERE id = ?", (order_id,))
    order = cursor.fetchone()
    if not order:
        conn.close()
        raise HTTPException(status_code=404, detail="Order not found")
    cursor.execute("SELECT * FROM order_items WHERE order_id = ?", (order_id,))
    items = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return {"order": dict(order), "items": items}
