"""
Neural POS — CSV Import Router
Bulk-import products from a CSV file.

Expected columns (header row required):
  name*, sku*, price*, cost, category, stock_quantity,
  reorder_level, barcode, description, supplier_name

  * = required

Behaviour:
  - New SKU  → INSERT
  - Existing SKU → UPDATE (price, cost, stock, category etc.)
  - supplier_name → auto-matched or created in suppliers table
  - Returns a detailed import report (created, updated, skipped, errors)

GET  /api/import/template   → download blank CSV template
POST /api/import/products   → upload + process CSV
GET  /api/import/history    → last 20 import runs
"""

import io
import csv
import json
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, UploadFile, File
from fastapi.responses import Response

from models.database import get_db
from routers.auth import get_current_user

logger = logging.getLogger("neural_pos.import")
router = APIRouter()

TEMPLATE_HEADERS = [
    "name", "sku", "price", "cost", "category",
    "stock_quantity", "reorder_level", "barcode",
    "description", "supplier_name"
]

TEMPLATE_EXAMPLE = [
    "Mineral Water 500ml", "SKU-001", "50", "30", "Beverages",
    "100", "20", "6001007002345", "500ml bottled water", "Aqua Distributors"
]


def ensure_import_table():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS import_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            filename    TEXT,
            total_rows  INTEGER DEFAULT 0,
            created     INTEGER DEFAULT 0,
            updated     INTEGER DEFAULT 0,
            skipped     INTEGER DEFAULT 0,
            errors      INTEGER DEFAULT 0,
            error_log   TEXT DEFAULT '[]',
            imported_by TEXT,
            imported_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    conn.close()

ensure_import_table()


def get_or_create_supplier(conn, name: str) -> Optional[int]:
    if not name or not name.strip():
        return None
    name = name.strip()
    row = conn.execute(
        "SELECT id FROM suppliers WHERE name = ? AND is_active = 1",
        (name,)
    ).fetchone()
    if row:
        return row["id"]
    # Create minimal supplier record
    conn.execute(
        "INSERT INTO suppliers (name) VALUES (?)", (name,)
    )
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def clean_float(val: str, default: float = 0.0) -> float:
    try:
        return float(str(val).replace(",", "").strip())
    except (ValueError, TypeError):
        return default


def clean_int(val: str, default: int = 0) -> int:
    try:
        return int(float(str(val).replace(",", "").strip()))
    except (ValueError, TypeError):
        return default


@router.get("/template")
async def download_template(request: Request):
    """Return a blank CSV template the user can fill in."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(401, "Authentication required")

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(TEMPLATE_HEADERS)
    writer.writerow(TEMPLATE_EXAMPLE)
    # 4 blank rows for the user to fill
    for _ in range(4):
        writer.writerow([""] * len(TEMPLATE_HEADERS))

    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={
            "Content-Disposition":
                "attachment; filename=neural_pos_product_import_template.csv"
        }
    )


@router.post("/products")
async def import_products(
    request: Request,
    file: UploadFile = File(...),
):
    """
    Upload a CSV file to bulk-import / update products.
    Returns a full import report.
    """
    user = get_current_user(request)
    if not user or user.get("role") not in ("admin", "manager"):
        raise HTTPException(403, "Manager/Admin required")

    if not file.filename.endswith(".csv"):
        raise HTTPException(400, "Only .csv files are supported")

    raw = await file.read()
    if len(raw) > 5 * 1024 * 1024:   # 5 MB limit
        raise HTTPException(400, "File too large (max 5 MB)")

    # Decode — try UTF-8 then Latin-1
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")

    reader = csv.DictReader(io.StringIO(text))

    # Validate headers
    if not reader.fieldnames:
        raise HTTPException(400, "CSV file is empty or has no header row")

    headers = [h.strip().lower() for h in reader.fieldnames]
    required = {"name", "sku", "price"}
    missing = required - set(headers)
    if missing:
        raise HTTPException(
            400,
            f"Missing required columns: {', '.join(sorted(missing))}. "
            f"Required: name, sku, price"
        )

    # Process rows
    created = 0
    updated = 0
    skipped = 0
    errors  = []
    total   = 0
    now     = datetime.now().isoformat()

    conn = get_db()
    try:
        for i, raw_row in enumerate(reader, start=2):   # row 1 = header
            total += 1
            # Normalise keys
            row = {k.strip().lower(): (v or "").strip() for k, v in raw_row.items()}

            name = row.get("name", "").strip()
            sku  = row.get("sku",  "").strip()

            if not name or not sku:
                skipped += 1
                errors.append({"row": i, "sku": sku or "—",
                                "issue": "Missing name or SKU — skipped"})
                continue

            price = clean_float(row.get("price", "0"))
            if price <= 0:
                errors.append({"row": i, "sku": sku,
                                "issue": f"Invalid price '{row.get('price')}' — defaulted to 0"})

            cost        = clean_float(row.get("cost", "0"))
            category    = row.get("category", "General").strip() or "General"
            stock       = clean_int(row.get("stock_quantity", "0"))
            reorder     = clean_int(row.get("reorder_level", "10"))
            barcode     = row.get("barcode", "").strip() or None
            description = row.get("description", "").strip() or None
            supplier_id = get_or_create_supplier(conn, row.get("supplier_name", ""))

            existing = conn.execute(
                "SELECT id FROM products WHERE sku = ?", (sku,)
            ).fetchone()

            if existing:
                conn.execute("""
                    UPDATE products SET
                        name = ?, price = ?, cost = ?, category = ?,
                        stock_quantity = ?, reorder_level = ?,
                        barcode = ?, description = ?,
                        supplier_id = COALESCE(?, supplier_id),
                        updated_at = ?
                    WHERE sku = ?
                """, (name, price, cost, category, stock, reorder,
                      barcode, description, supplier_id, now, sku))
                updated += 1
            else:
                try:
                    conn.execute("""
                        INSERT INTO products
                            (sku, name, price, cost, category, stock_quantity,
                             reorder_level, barcode, description, supplier_id,
                             is_active, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                    """, (sku, name, price, cost, category, stock, reorder,
                          barcode, description, supplier_id, now, now))
                    created += 1
                except Exception as e:
                    errors.append({"row": i, "sku": sku, "issue": str(e)})
                    skipped += 1
                    total -= 1
                    continue

        conn.commit()

        # Save history
        conn.execute("""
            INSERT INTO import_history
                (filename, total_rows, created, updated, skipped, errors,
                 error_log, imported_by, imported_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        """, (file.filename, total, created, updated, skipped,
              len(errors), json.dumps(errors),
              user.get("username") or user.get("full_name")))
        conn.commit()

    except Exception as e:
        conn.close()
        logger.error(f"CSV import error: {e}", exc_info=True)
        raise HTTPException(500, f"Import failed: {e}")

    conn.close()

    return {
        "success":  True,
        "filename": file.filename,
        "summary": {
            "total_rows": total,
            "created":    created,
            "updated":    updated,
            "skipped":    skipped,
            "errors":     len(errors),
        },
        "error_log": errors[:50],   # cap response size
        "message": (
            f"Import complete: {created} created, {updated} updated, "
            f"{skipped} skipped, {len(errors)} errors."
        )
    }


@router.get("/history")
async def import_history(request: Request):
    user = get_current_user(request)
    if not user or user.get("role") not in ("admin", "manager"):
        raise HTTPException(403, "Manager/Admin required")
    conn = get_db()
    rows = conn.execute("""
        SELECT id, filename, total_rows, created, updated, skipped,
               errors, imported_by, imported_at
        FROM import_history
        ORDER BY imported_at DESC LIMIT 20
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]
