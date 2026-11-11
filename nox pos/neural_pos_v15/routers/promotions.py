"""
Neural POS — Promotions Engine
Buy X get Y free, percentage off category, fixed discount codes
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional, List
from models.database import get_db
from routers.auth import get_current_user
import datetime, json

router = APIRouter()

class PromotionCreate(BaseModel):
    name: str
    type: str           # percent_off | fixed_off | bxgy | category_percent
    value: float        # discount value or buy qty
    get_qty: Optional[int] = 1       # for BXGY: get N free
    category: Optional[str] = ""     # for category_percent
    product_ids: Optional[str] = ""  # JSON list of product IDs
    min_order: Optional[float] = 0
    code: Optional[str] = ""
    start_date: Optional[str] = ""
    end_date: Optional[str] = ""
    is_active: Optional[int] = 1


@router.get("")
async def list_promotions(current_user=Depends(get_current_user)):
    db = get_db()
    rows = db.execute("SELECT * FROM promotions ORDER BY is_active DESC, created_at DESC").fetchall()
    db.close()
    return [dict(r) for r in rows]

@router.post("")
async def create_promotion(data: PromotionCreate, current_user=Depends(get_current_user)):
    if current_user.get("role") not in ["admin","manager"]:
        raise HTTPException(403, "Manager or Admin required")
    db = get_db()
    db.execute("""INSERT INTO promotions
        (name,type,value,get_qty,category,product_ids,min_order,code,start_date,end_date,is_active)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (data.name, data.type, data.value, data.get_qty or 1,
         data.category or "", data.product_ids or "",
         data.min_order or 0, data.code or "",
         data.start_date or "", data.end_date or "", data.is_active or 1))
    pid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.commit(); db.close()
    return {"success": True, "id": pid}

@router.put("/{promo_id}")
async def update_promotion(promo_id: int, data: PromotionCreate, current_user=Depends(get_current_user)):
    if current_user.get("role") not in ["admin","manager"]:
        raise HTTPException(403, "Manager or Admin required")
    db = get_db()
    db.execute("""UPDATE promotions SET
        name=?,type=?,value=?,get_qty=?,category=?,product_ids=?,
        min_order=?,code=?,start_date=?,end_date=?,is_active=?
        WHERE id=?""",
        (data.name, data.type, data.value, data.get_qty or 1,
         data.category or "", data.product_ids or "",
         data.min_order or 0, data.code or "",
         data.start_date or "", data.end_date or "",
         data.is_active, promo_id))
    db.commit(); db.close()
    return {"updated": True}

@router.delete("/{promo_id}")
async def delete_promotion(promo_id: int, current_user=Depends(get_current_user)):
    if current_user.get("role") not in ["admin","manager"]:
        raise HTTPException(403, "Manager or Admin required")
    db = get_db()
    db.execute("UPDATE promotions SET is_active=0 WHERE id=?", (promo_id,))
    db.commit(); db.close()
    return {"deleted": True}

@router.post("/apply")
async def apply_promotion(body: dict, current_user=Depends(get_current_user)):
    """Apply a promo code or auto-apply active promotions to a cart."""
    code = body.get("code","").upper()
    cart_total = float(body.get("cart_total", 0))
    cart_items = body.get("items", [])   # [{product_id, quantity, category, price}]
    today = datetime.date.today().isoformat()

    db = get_db()
    promos = db.execute("""
        SELECT * FROM promotions WHERE is_active=1
        AND (start_date='' OR start_date <= ?)
        AND (end_date=''   OR end_date   >= ?)
    """, (today, today)).fetchall()
    db.close()

    discount = 0.0
    applied = []

    for p in promos:
        p = dict(p)
        # Code-required promos
        if p["code"] and p["code"].upper() != code:
            continue
        if p["min_order"] > 0 and cart_total < p["min_order"]:
            continue

        if p["type"] == "percent_off":
            d = round(cart_total * p["value"] / 100, 2)
            discount += d; applied.append(f"{p['name']}: -{p['value']}%")

        elif p["type"] == "fixed_off":
            discount += p["value"]; applied.append(f"{p['name']}: -KES {p['value']}")

        elif p["type"] == "category_percent" and p["category"]:
            cat_total = sum(i["price"]*i["quantity"] for i in cart_items
                           if i.get("category","").lower() == p["category"].lower())
            d = round(cat_total * p["value"] / 100, 2)
            if d > 0: discount += d; applied.append(f"{p['name']}: -{p['value']}% on {p['category']}")

        elif p["type"] == "bxgy":
            buy_qty = int(p["value"])
            get_qty = int(p["get_qty"] or 1)
            total_qty = sum(i["quantity"] for i in cart_items)
            free_sets = total_qty // (buy_qty + get_qty)
            if free_sets > 0:
                # Free items = lowest-priced items
                prices = sorted([i["price"] for i in cart_items for _ in range(i["quantity"])])
                free_total = sum(prices[:free_sets * get_qty])
                discount += free_total
                applied.append(f"{p['name']}: {free_sets*get_qty} free item(s)")

    return {
        "discount": round(min(discount, cart_total), 2),
        "applied_promotions": applied,
        "code_valid": bool(code and any(p["code"].upper()==code for p in [dict(x) for x in promos]))
    }
