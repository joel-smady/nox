"""
Neural POS — Product Bundles Router
Sell combo products (e.g. "Meal Deal", "Office Pack") as a single SKU.
Each sale automatically deducts stock from every component product.

Endpoints:
  GET    /api/bundles                  list all bundles
  POST   /api/bundles                  create bundle
  GET    /api/bundles/{id}             single bundle + components
  PUT    /api/bundles/{id}             update bundle
  DELETE /api/bundles/{id}             deactivate bundle
  GET    /api/bundles/{id}/stock       check if bundle is in stock
  POST   /api/bundles/{id}/sell        sell bundle (deduct all components)
"""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime
from models.database import get_db
from routers.auth import get_current_user

def _model_to_dict(model) -> dict:
    """Pydantic v1/v2 compatible model serialization."""
    if hasattr(model, 'model_dump'):
        return model.model_dump()
    return model.dict()


router = APIRouter()


# ── Models ────────────────────────────────────────────────────────────────────

class BundleComponent(BaseModel):
    product_id: int
    quantity: float          # float to support e.g. 0.5 kg

class BundleCreate(BaseModel):
    name: str
    description: Optional[str] = ""
    sku: Optional[str] = None
    price: float             # bundle selling price (can be < sum of parts)
    category: Optional[str] = "Bundle"
    is_active: Optional[int] = 1
    components: List[BundleComponent]

class BundleUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    price: Optional[float] = None
    category: Optional[str] = None
    is_active: Optional[int] = None
    components: Optional[List[BundleComponent]] = None


# ── DB setup ──────────────────────────────────────────────────────────────────

def ensure_bundle_tables():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bundles (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL,
            description TEXT DEFAULT '',
            sku         TEXT UNIQUE,
            price       REAL NOT NULL,
            category    TEXT DEFAULT 'Bundle',
            is_active   INTEGER DEFAULT 1,
            created_at  TEXT DEFAULT (datetime('now')),
            updated_at  TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bundle_components (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            bundle_id  INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            quantity   REAL NOT NULL DEFAULT 1,
            FOREIGN KEY (bundle_id)  REFERENCES bundles(id)  ON DELETE CASCADE,
            FOREIGN KEY (product_id) REFERENCES products(id),
            UNIQUE (bundle_id, product_id)
        )
    """)
    conn.commit()
    conn.close()

ensure_bundle_tables()


# ── Helpers ───────────────────────────────────────────────────────────────────

def bundle_with_components(conn, bundle_id: int) -> dict:
    b = conn.execute("SELECT * FROM bundles WHERE id = ?", (bundle_id,)).fetchone()
    if not b:
        return None
    bundle = dict(b)
    comps = conn.execute("""
        SELECT bc.product_id, bc.quantity,
               p.name as product_name, p.sku as product_sku,
               p.price as unit_price, p.cost as unit_cost,
               p.stock_quantity, p.category
        FROM bundle_components bc
        JOIN products p ON bc.product_id = p.id
        WHERE bc.bundle_id = ?
        ORDER BY p.name
    """, (bundle_id,)).fetchall()
    bundle["components"] = [dict(c) for c in comps]

    # Derived fields
    bundle["components_cost"] = sum(
        c["unit_cost"] * c["quantity"] for c in bundle["components"]
    )
    bundle["components_retail"] = sum(
        c["unit_price"] * c["quantity"] for c in bundle["components"]
    )
    bundle["savings"] = round(bundle["components_retail"] - bundle["price"], 2)
    bundle["margin"] = round(
        (bundle["price"] - bundle["components_cost"]) / bundle["price"] * 100, 1
    ) if bundle["price"] else 0

    # Max sellable = floor of (min stock / required qty) across all components
    if bundle["components"]:
        bundle["max_stock"] = int(min(
            c["stock_quantity"] // c["quantity"]
            for c in bundle["components"]
        ))
    else:
        bundle["max_stock"] = 0

    return bundle


def auto_sku(conn, name: str) -> str:
    prefix = "BND-" + "".join(
        c.upper() for c in name if c.isalpha()
    )[:4]
    count = conn.execute("SELECT COUNT(*) FROM bundles").fetchone()[0]
    return f"{prefix}-{count + 1:03d}"


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("")
async def list_bundles(request: Request, active_only: bool = True):
    user = get_current_user(request)
    if not user:
        raise HTTPException(401, "Authentication required")
    conn = get_db()
    q = "SELECT id FROM bundles"
    if active_only:
        q += " WHERE is_active = 1"
    q += " ORDER BY name"
    rows = conn.execute(q).fetchall()
    bundles = [bundle_with_components(conn, r["id"]) for r in rows]
    conn.close()
    return bundles


@router.get("/{bundle_id}")
async def get_bundle(bundle_id: int, request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(401, "Authentication required")
    conn = get_db()
    b = bundle_with_components(conn, bundle_id)
    conn.close()
    if not b:
        raise HTTPException(404, "Bundle not found")
    return b


@router.post("")
async def create_bundle(data: BundleCreate, request: Request):
    user = get_current_user(request)
    if not user or user.get("role") not in ("admin", "manager"):
        raise HTTPException(403, "Manager/Admin required")
    if not data.components:
        raise HTTPException(400, "Bundle must have at least one component")

    conn = get_db()
    try:
        sku = data.sku or auto_sku(conn, data.name)
        conn.execute("""
            INSERT INTO bundles (name, description, sku, price, category, is_active)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (data.name, data.description or "", sku,
              data.price, data.category or "Bundle", data.is_active))
        bundle_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        for comp in data.components:
            # Validate product exists
            p = conn.execute(
                "SELECT id FROM products WHERE id = ? AND is_active = 1",
                (comp.product_id,)
            ).fetchone()
            if not p:
                conn.close()
                raise HTTPException(404, f"Product {comp.product_id} not found")
            conn.execute("""
                INSERT INTO bundle_components (bundle_id, product_id, quantity)
                VALUES (?, ?, ?)
            """, (bundle_id, comp.product_id, comp.quantity))

        conn.commit()
        result = bundle_with_components(conn, bundle_id)
        conn.close()
        return {"success": True, "bundle": result}
    except HTTPException:
        conn.close()
        raise
    except Exception as e:
        conn.close()
        raise HTTPException(400, str(e))


@router.put("/{bundle_id}")
async def update_bundle(bundle_id: int, data: BundleUpdate, request: Request):
    user = get_current_user(request)
    if not user or user.get("role") not in ("admin", "manager"):
        raise HTTPException(403, "Manager/Admin required")

    conn = get_db()
    if not conn.execute("SELECT id FROM bundles WHERE id = ?", (bundle_id,)).fetchone():
        conn.close()
        raise HTTPException(404, "Bundle not found")

    fields = {k: v for k, v in _model_to_dict(data).items()
              if v is not None and k != "components"}
    if fields:
        fields["updated_at"] = datetime.now().isoformat()
        sets = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(
            f"UPDATE bundles SET {sets} WHERE id = ?",
            list(fields.values()) + [bundle_id]
        )

    if data.components is not None:
        conn.execute(
            "DELETE FROM bundle_components WHERE bundle_id = ?", (bundle_id,)
        )
        for comp in data.components:
            conn.execute("""
                INSERT INTO bundle_components (bundle_id, product_id, quantity)
                VALUES (?, ?, ?)
            """, (bundle_id, comp.product_id, comp.quantity))

    conn.commit()
    result = bundle_with_components(conn, bundle_id)
    conn.close()
    return {"success": True, "bundle": result}


@router.delete("/{bundle_id}")
async def delete_bundle(bundle_id: int, request: Request):
    user = get_current_user(request)
    if not user or user.get("role") not in ("admin", "manager"):
        raise HTTPException(403, "Manager/Admin required")
    conn = get_db()
    conn.execute(
        "UPDATE bundles SET is_active = 0 WHERE id = ?", (bundle_id,)
    )
    conn.commit()
    conn.close()
    return {"success": True}


@router.get("/{bundle_id}/stock")
async def bundle_stock(bundle_id: int, request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(401, "Authentication required")
    conn = get_db()
    b = bundle_with_components(conn, bundle_id)
    conn.close()
    if not b:
        raise HTTPException(404, "Bundle not found")

    short = [
        {
            "product_name": c["product_name"],
            "required": c["quantity"],
            "available": c["stock_quantity"],
        }
        for c in b["components"]
        if c["stock_quantity"] < c["quantity"]
    ]

    return {
        "bundle_id":  bundle_id,
        "max_stock":  b["max_stock"],
        "in_stock":   b["max_stock"] > 0,
        "shortfalls": short,
    }


@router.post("/{bundle_id}/sell")
async def sell_bundle(bundle_id: int, request: Request):
    """
    Sell one unit of a bundle — deducts each component from stock.
    Called automatically by the POS when a bundle SKU is added to cart.
    """
    user = get_current_user(request)
    if not user:
        raise HTTPException(401, "Authentication required")

    body = await request.json()
    qty = int(body.get("quantity", 1))

    conn = get_db()
    b = bundle_with_components(conn, bundle_id)
    if not b:
        conn.close()
        raise HTTPException(404, "Bundle not found")

    if b["max_stock"] < qty:
        conn.close()
        raise HTTPException(400,
            f"Insufficient stock. Max available: {b['max_stock']}")

    now = datetime.now().isoformat()
    for comp in b["components"]:
        deduct = comp["quantity"] * qty
        conn.execute("""
            UPDATE products
            SET stock_quantity = stock_quantity - ?,
                updated_at = ?
            WHERE id = ?
        """, (deduct, now, comp["product_id"]))

    conn.commit()
    result = bundle_with_components(conn, bundle_id)
    conn.close()
    return {
        "success": True,
        "bundle":  result,
        "qty_sold": qty,
        "message": f"Sold {qty}× {b['name']} — {len(b['components'])} components deducted"
    }
