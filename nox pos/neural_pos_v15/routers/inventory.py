"""
Neural POS — Inventory Router
Full inventory management with stock tracking
"""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
from models.database import get_db
from routers.auth import get_current_user

def _model_to_dict(model) -> dict:
    """Pydantic v1/v2 compatible model serialization."""
    if hasattr(model, 'model_dump'):
        return model.model_dump()
    return model.dict()


router = APIRouter()

class ProductCreate(BaseModel):
    sku: str
    name: str
    description: Optional[str] = None
    category: str
    price: float
    cost: float
    stock_quantity: int = 0
    reorder_level: int = 10
    supplier_id: Optional[int] = None
    barcode: Optional[str] = None

class ProductUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str] = None
    price: Optional[float] = None
    cost: Optional[float] = None
    reorder_level: Optional[int] = None
    supplier_id: Optional[int] = None
    barcode: Optional[str] = None
    is_active: Optional[int] = None

class StockAdjustment(BaseModel):
    product_id: int
    adjustment: int
    reason: str

@router.get("/products")
async def list_products(request: Request, category: str = "", search: str = "", low_stock: bool = False):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")

    conn = get_db()
    cursor = conn.cursor()
    query = """
        SELECT p.*, s.name as supplier_name
        FROM products p
        LEFT JOIN suppliers s ON p.supplier_id = s.id
        WHERE 1=1
    """
    params = []

    if category:
        query += " AND p.category = ?"
        params.append(category)
    if search:
        query += " AND (p.name LIKE ? OR p.sku LIKE ?)"
        params.extend([f"%{search}%", f"%{search}%"])
    if low_stock:
        query += " AND p.stock_quantity <= p.reorder_level"

    query += " ORDER BY p.category, p.name"
    cursor.execute(query, params)
    products = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return products

@router.get("/categories")
async def get_categories(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT category FROM products WHERE is_active = 1 ORDER BY category")
    cats = [row[0] for row in cursor.fetchall()]
    conn.close()
    return cats

@router.post("/products")
async def create_product(product: ProductCreate, request: Request):
    user = get_current_user(request)
    if not user or user["role"] not in ["admin", "manager"]:
        raise HTTPException(status_code=403, detail="Manager or Admin access required")

    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO products (sku, name, description, category, price, cost,
                                  stock_quantity, reorder_level, supplier_id, barcode)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (product.sku, product.name, product.description, product.category,
              product.price, product.cost, product.stock_quantity, product.reorder_level,
              product.supplier_id, product.barcode))
        product_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return {"success": True, "product_id": product_id, "message": "Product created"}
    except Exception as e:
        conn.close()
        raise HTTPException(status_code=400, detail=str(e))

@router.put("/products/{product_id}")
async def update_product(product_id: int, product: ProductUpdate, request: Request):
    user = get_current_user(request)
    if not user or user["role"] not in ["admin", "manager"]:
        raise HTTPException(status_code=403, detail="Manager or Admin access required")

    conn = get_db()
    cursor = conn.cursor()
    updates = {k: v for k, v in _model_to_dict(product).items() if v is not None}
    if not updates:
        conn.close()
        return {"message": "No updates provided"}

    updates["updated_at"] = datetime.now().isoformat()
    set_clause = ", ".join([f"{k} = ?" for k in updates.keys()])
    cursor.execute(f"UPDATE products SET {set_clause} WHERE id = ?", list(updates.values()) + [product_id])
    conn.commit()
    conn.close()
    return {"success": True, "message": "Product updated"}

@router.delete("/products/{product_id}")
async def delete_product(product_id: int, request: Request):
    user = get_current_user(request)
    if not user or user["role"] not in ["admin"]:
        raise HTTPException(status_code=403, detail="Admin access required")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE products SET is_active = 0 WHERE id = ?", (product_id,))
    conn.commit()
    conn.close()
    return {"success": True, "message": "Product deactivated"}

@router.post("/adjust-stock")
async def adjust_stock(adjustment: StockAdjustment, request: Request):
    user = get_current_user(request)
    if not user or user["role"] not in ["admin", "manager"]:
        raise HTTPException(status_code=403, detail="Manager or Admin access required")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE products
        SET stock_quantity = stock_quantity + ?, updated_at = ?
        WHERE id = ?
    """, (adjustment.adjustment, datetime.now().isoformat(), adjustment.product_id))
    conn.commit()
    cursor.execute("SELECT stock_quantity FROM products WHERE id = ?", (adjustment.product_id,))
    row = cursor.fetchone()
    conn.close()
    return {"success": True, "new_quantity": row["stock_quantity"] if row else 0}

@router.get("/low-stock")
async def get_low_stock(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT p.*, s.name as supplier_name
        FROM products p LEFT JOIN suppliers s ON p.supplier_id = s.id
        WHERE p.stock_quantity <= p.reorder_level AND p.is_active = 1
        ORDER BY p.stock_quantity ASC
    """)
    items = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return items

@router.get("/stats")
async def inventory_stats(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) as total, SUM(stock_quantity * cost) as total_value FROM products WHERE is_active = 1")
    row = cursor.fetchone()
    cursor.execute("SELECT COUNT(*) as low FROM products WHERE stock_quantity <= reorder_level AND is_active = 1")
    low = cursor.fetchone()
    cursor.execute("SELECT COUNT(DISTINCT category) as cats FROM products WHERE is_active = 1")
    cats = cursor.fetchone()
    conn.close()
    return {
        "total_products": row["total"],
        "total_value": round(row["total_value"] or 0, 2),
        "low_stock_count": low["low"],
        "categories": cats["cats"]
    }
