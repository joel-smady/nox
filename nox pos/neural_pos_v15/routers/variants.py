"""
Neural POS — Product Variants
Sizes, colors, SKUs with individual stock per variant
"""
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional, List
from models.database import get_db
from routers.auth import get_current_user

router = APIRouter()

class VariantCreate(BaseModel):
    product_id: int
    name: str          # e.g. "Red / XL"
    sku: Optional[str] = ""
    barcode: Optional[str] = ""
    price_adjustment: Optional[float] = 0.0   # +/- from base price
    stock_quantity: Optional[int] = 0
    attributes: Optional[str] = ""            # JSON string e.g. '{"size":"XL","color":"Red"}'


COLS = "id,product_id,name,sku,barcode,price_adjustment,stock_quantity,attributes,is_active"

@router.get("/product/{product_id}")
async def get_variants(product_id: int, current_user=Depends(get_current_user)):
    db = get_db()
    # Get base product price
    prod = db.execute("SELECT name, price FROM products WHERE id=?", (product_id,)).fetchone()
    if not prod:
        db.close(); raise HTTPException(404, "Product not found")
    rows = db.execute(
        f"SELECT {COLS} FROM product_variants WHERE product_id=? AND is_active=1 ORDER BY name",
        (product_id,)
    ).fetchall()
    db.close()
    variants = []
    for r in rows:
        v = dict(zip(COLS.split(","), r))
        v["effective_price"] = round(prod[1] + (v["price_adjustment"] or 0), 2)
        v["product_name"] = prod[0]
        variants.append(v)
    return variants

@router.post("")
async def create_variant(data: VariantCreate, current_user=Depends(get_current_user)):
    if current_user.get("role") not in ["admin","manager"]:
        raise HTTPException(403, "Manager or Admin required")
    db = get_db()
    db.execute(
        "INSERT INTO product_variants (product_id,name,sku,barcode,price_adjustment,stock_quantity,attributes) VALUES (?,?,?,?,?,?,?)",
        (data.product_id, data.name, data.sku or "", data.barcode or "",
         data.price_adjustment or 0, data.stock_quantity or 0, data.attributes or "")
    )
    vid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.commit()
    row = db.execute(f"SELECT {COLS} FROM product_variants WHERE id=?", (vid,)).fetchone()
    db.close()
    return dict(zip(COLS.split(","), row))

@router.put("/{variant_id}")
async def update_variant(variant_id: int, data: VariantCreate, current_user=Depends(get_current_user)):
    if current_user.get("role") not in ["admin","manager"]:
        raise HTTPException(403, "Manager or Admin required")
    db = get_db()
    db.execute(
        "UPDATE product_variants SET name=?,sku=?,barcode=?,price_adjustment=?,stock_quantity=?,attributes=? WHERE id=?",
        (data.name, data.sku or "", data.barcode or "",
         data.price_adjustment or 0, data.stock_quantity or 0, data.attributes or "", variant_id)
    )
    db.commit()
    row = db.execute(f"SELECT {COLS} FROM product_variants WHERE id=?", (variant_id,)).fetchone()
    db.close()
    return dict(zip(COLS.split(","), row))

@router.delete("/{variant_id}")
async def delete_variant(variant_id: int, current_user=Depends(get_current_user)):
    if current_user.get("role") not in ["admin","manager"]:
        raise HTTPException(403, "Manager or Admin required")
    db = get_db()
    db.execute("UPDATE product_variants SET is_active=0 WHERE id=?", (variant_id,))
    db.commit(); db.close()
    return {"deleted": True}

@router.post("/{variant_id}/adjust-stock")
async def adjust_variant_stock(variant_id: int, body: dict, current_user=Depends(get_current_user)):
    db = get_db()
    amount = int(body.get("amount", 0))
    db.execute(
        "UPDATE product_variants SET stock_quantity = MAX(0, stock_quantity + ?) WHERE id=?",
        (amount, variant_id)
    )
    db.commit()
    row = db.execute("SELECT stock_quantity FROM product_variants WHERE id=?", (variant_id,)).fetchone()
    db.close()
    return {"stock_quantity": row[0] if row else 0}

@router.get("/barcode/{barcode}")
async def lookup_by_barcode(barcode: str, current_user=Depends(get_current_user)):
    """Look up product OR variant by barcode — used by barcode scanner"""
    db = get_db()
    # Check variants first
    vrow = db.execute("""
        SELECT v.id, v.product_id, v.name, v.sku, v.barcode, v.price_adjustment,
               v.stock_quantity, p.name as product_name, p.price, p.category
        FROM product_variants v JOIN products p ON v.product_id=p.id
        WHERE v.barcode=? AND v.is_active=1
    """, (barcode,)).fetchone()
    if vrow:
        db.close()
        return {
            "type": "variant",
            "id": vrow[0], "product_id": vrow[1],
            "name": f"{vrow[7]} — {vrow[2]}",
            "sku": vrow[3], "barcode": vrow[4],
            "price": round(vrow[8] + vrow[5], 2),
            "stock_quantity": vrow[6], "category": vrow[9]
        }
    # Check main products
    prow = db.execute(
        "SELECT id,sku,name,category,price,stock_quantity,barcode FROM products WHERE barcode=? AND is_active=1",
        (barcode,)
    ).fetchone()
    if prow:
        db.close()
        return {
            "type": "product",
            "id": prow[0], "product_id": prow[0],
            "name": prow[2], "sku": prow[1],
            "barcode": prow[6], "price": prow[4],
            "stock_quantity": prow[5], "category": prow[3]
        }
    # Try SKU lookup too
    srow = db.execute(
        "SELECT id,sku,name,category,price,stock_quantity,barcode FROM products WHERE sku=? AND is_active=1",
        (barcode,)
    ).fetchone()
    db.close()
    if srow:
        return {
            "type": "product", "id": srow[0], "product_id": srow[0],
            "name": srow[2], "sku": srow[1], "barcode": srow[6] or "",
            "price": srow[4], "stock_quantity": srow[5], "category": srow[3]
        }
    raise HTTPException(404, f"No product found for barcode: {barcode}")
