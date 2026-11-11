"""
Neural POS — Barcode Label Router
Generates printable barcode labels as PDF or PNG.

Supported formats: Code128, EAN13, EAN8, QR Code
Output: PDF (print-ready, A4 sheet with multiple labels) or PNG (single label)

Dependencies:
  python-barcode   — 1D barcodes (Code128, EAN13, EAN8)
  qrcode           — QR codes
  Pillow           — image composition
  reportlab        — PDF generation
"""

import io
import os
import logging
from typing import List, Optional
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel

from models.database import get_db
from routers.auth import get_current_user

logger = logging.getLogger("neural_pos.barcodes")
router = APIRouter()


# ── Models ────────────────────────────────────────────────────────────────────

class LabelRequest(BaseModel):
    product_ids: List[int]
    format: Optional[str] = "pdf"          # "pdf" | "png"
    barcode_type: Optional[str] = "code128" # "code128" | "ean13" | "ean8" | "qr"
    label_width_mm:  Optional[float] = 60
    label_height_mm: Optional[float] = 30
    per_row:  Optional[int] = 3
    show_name: Optional[bool] = True
    show_price: Optional[bool] = True
    show_sku:   Optional[bool] = True
    copies_per_product: Optional[int] = 1


# ── Barcode generation ────────────────────────────────────────────────────────

def generate_barcode_image(code: str, barcode_type: str = "code128") -> bytes:
    """
    Generate a barcode image (PNG bytes) for a given code string.
    Returns PNG bytes.
    """
    barcode_type = barcode_type.lower()

    if barcode_type == "qr":
        try:
            import qrcode
            qr = qrcode.QRCode(version=1, box_size=4, border=1)
            qr.add_data(code)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue()
        except ImportError:
            raise RuntimeError("qrcode not installed. Run: pip install qrcode[pil]")

    # 1D barcodes via python-barcode
    try:
        import barcode
        from barcode.writer import ImageWriter

        type_map = {
            "code128": "code128",
            "ean13":   "ean13",
            "ean8":    "ean8",
            "code39":  "code39",
        }
        bc_class_name = type_map.get(barcode_type, "code128")

        # EAN13 needs exactly 12 digits (13th is checksum)
        if bc_class_name == "ean13":
            digits = "".join(c for c in code if c.isdigit())
            code = digits[:12].ljust(12, "0")
        elif bc_class_name == "ean8":
            digits = "".join(c for c in code if c.isdigit())
            code = digits[:7].ljust(7, "0")

        writer = ImageWriter()
        bc = barcode.get(bc_class_name, code, writer=writer)
        buf = io.BytesIO()
        bc.write(buf, options={
            "module_height": 8.0,
            "module_width":  0.2,
            "quiet_zone":    1.0,
            "font_size":     6,
            "text_distance": 1.5,
            "background":    "white",
            "foreground":    "black",
        })
        buf.seek(0)
        return buf.read()

    except ImportError:
        raise RuntimeError("python-barcode not installed. Run: pip install python-barcode Pillow")


def compose_label_image(
    product: dict,
    barcode_type: str,
    width_px: int,
    height_px: int,
    show_name: bool,
    show_price: bool,
    show_sku: bool,
    currency: str = "KES",
) -> bytes:
    """
    Compose a single product label as PNG bytes.
    Layout: barcode centred, name/price/sku as text lines below.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        raise RuntimeError("Pillow not installed. Run: pip install Pillow")

    # Create white canvas
    img = Image.new("RGB", (width_px, height_px), "white")
    draw = ImageDraw.Draw(img)

    # Text lines to show
    text_lines = []
    if show_name:
        name = product.get("name", "")
        text_lines.append(("name", name[:28]))
    if show_sku:
        text_lines.append(("sku", f"SKU: {product.get('sku', '')}"))
    if show_price:
        price = product.get("price", 0)
        text_lines.append(("price", f"{currency} {price:,.2f}"))

    # Estimate text area height (approx 12px per line)
    text_area_h = len(text_lines) * 14 + 4
    barcode_area_h = height_px - text_area_h - 4

    # Get barcode code to use
    bc_code = product.get("barcode") or product.get("sku") or str(product["id"])

    try:
        bc_bytes = generate_barcode_image(bc_code, barcode_type)
        bc_img = Image.open(io.BytesIO(bc_bytes)).convert("RGB")
        # Scale barcode to fit available height
        bc_w = int(bc_img.width * (barcode_area_h / bc_img.height))
        bc_w = min(bc_w, width_px - 10)
        bc_img = bc_img.resize((bc_w, barcode_area_h), Image.LANCZOS)
        # Centre the barcode
        x_off = (width_px - bc_w) // 2
        img.paste(bc_img, (x_off, 2))
    except Exception as e:
        # Draw error text if barcode fails
        draw.text((4, 4), f"[{bc_code}]", fill="black")

    # Draw text lines
    y = barcode_area_h + 4
    for kind, line in text_lines:
        # Use larger font for price
        font_size = 11 if kind == "price" else 9
        font = None
        _font_candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",          # Debian/Ubuntu/Mint
            "/usr/share/fonts/truetype/freefont/FreeSans.ttf",          # Fallback Linux
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            "/System/Library/Fonts/Helvetica.ttc",                      # macOS
            "C:/Windows/Fonts/arial.ttf",                               # Windows
        ]
        for _fp in _font_candidates:
            try:
                font = ImageFont.truetype(_fp, font_size)
                break
            except Exception:
                continue
        if font is None:
            font = ImageFont.load_default()
        draw.text((4, y), line, fill="black", font=font)
        y += font_size + 3

    # Draw border
    draw.rectangle([(0, 0), (width_px - 1, height_px - 1)], outline="#cccccc", width=1)

    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def generate_label_pdf(
    products: List[dict],
    barcode_type: str,
    label_w_mm: float,
    label_h_mm: float,
    per_row: int,
    show_name: bool,
    show_price: bool,
    show_sku: bool,
    copies: int,
    currency: str = "KES",
) -> bytes:
    """Generate a print-ready A4 PDF with all product labels."""
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.units import mm
        from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Image as RLImage, Spacer
        from reportlab.lib import colors
    except ImportError:
        raise RuntimeError("reportlab not installed. Run: pip install reportlab")

    MM = mm
    page_w, page_h = A4
    margin = 10 * MM

    label_w = label_w_mm * MM
    label_h = label_h_mm * MM

    # px dimensions (96dpi equivalent)
    px_w = int(label_w_mm * 3.78)
    px_h = int(label_h_mm * 3.78)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=margin, rightMargin=margin,
        topMargin=margin, bottomMargin=margin
    )

    all_images = []
    for product in products:
        for _ in range(copies):
            try:
                png = compose_label_image(
                    product, barcode_type, px_w, px_h,
                    show_name, show_price, show_sku, currency
                )
                img_buf = io.BytesIO(png)
                rl_img  = RLImage(img_buf, width=label_w, height=label_h)
                all_images.append(rl_img)
            except Exception as e:
                logger.warning(f"Label generation failed for product {product.get('id')}: {e}")

    if not all_images:
        raise RuntimeError("No labels could be generated")

    # Arrange into rows
    rows = []
    for i in range(0, len(all_images), per_row):
        row = all_images[i:i + per_row]
        while len(row) < per_row:
            row.append("")   # empty cell
        rows.append(row)

    col_w = (page_w - 2 * margin) / per_row
    table = Table(rows, colWidths=[col_w] * per_row, rowHeights=[label_h] * len(rows))
    table.setStyle(TableStyle([
        ("ALIGN",     (0, 0), (-1, -1), "CENTER"),
        ("VALIGN",    (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING",  (0, 0), (-1, -1), 2),
        ("RIGHTPADDING", (0, 0), (-1, -1), 2),
        ("TOPPADDING",   (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 2),
    ]))

    doc.build([table])
    buf.seek(0)
    return buf.read()


# ── API endpoints ─────────────────────────────────────────────────────────────

@router.post("/generate")
async def generate_labels(body: LabelRequest, request: Request):
    """
    Generate barcode labels for one or more products.
    Returns PDF (default) or PNG for a single product.
    """
    user = get_current_user(request)
    if not user or user.get("role") not in ("admin", "manager"):
        raise HTTPException(403, "Manager/Admin required")

    if not body.product_ids:
        raise HTTPException(400, "No product IDs provided")

    # Fetch products
    conn = get_db()
    placeholders = ",".join("?" * len(body.product_ids))
    rows = conn.execute(
        f"SELECT * FROM products WHERE id IN ({placeholders}) AND is_active = 1",
        body.product_ids
    ).fetchall()
    # Get currency setting
    cur_row = conn.execute("SELECT value FROM settings WHERE key='currency'").fetchone()
    currency = cur_row["value"] if cur_row else "KES"
    conn.close()

    if not rows:
        raise HTTPException(404, "No products found")

    products = [dict(r) for r in rows]

    fmt = body.format.lower()

    # Single product PNG
    if fmt == "png" and len(products) == 1:
        try:
            px_w = int(body.label_width_mm  * 3.78)
            px_h = int(body.label_height_mm * 3.78)
            png = compose_label_image(
                products[0], body.barcode_type, px_w, px_h,
                body.show_name, body.show_price, body.show_sku, currency
            )
            return Response(
                content=png,
                media_type="image/png",
                headers={"Content-Disposition": f"inline; filename=label_{products[0]['id']}.png"}
            )
        except RuntimeError as e:
            raise HTTPException(503, str(e))

    # PDF (default — works for any number of products)
    try:
        pdf = generate_label_pdf(
            products,
            barcode_type       = body.barcode_type,
            label_w_mm         = body.label_width_mm,
            label_h_mm         = body.label_height_mm,
            per_row            = body.per_row,
            show_name          = body.show_name,
            show_price         = body.show_price,
            show_sku           = body.show_sku,
            copies             = body.copies_per_product,
            currency           = currency,
        )
        filename = "labels_single.pdf" if len(products) == 1 else f"labels_{len(products)}_products.pdf"
        return Response(
            content=pdf,
            media_type="application/pdf",
            headers={"Content-Disposition": f"inline; filename={filename}"}
        )
    except RuntimeError as e:
        raise HTTPException(503, str(e))


@router.get("/preview/{product_id}")
async def preview_label(
    product_id: int,
    request: Request,
    barcode_type: str = "code128",
    width_mm: float = 60,
    height_mm: float = 30,
):
    """Quick PNG preview of a single product label."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(401, "Authentication required")

    conn = get_db()
    row = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    cur = conn.execute("SELECT value FROM settings WHERE key='currency'").fetchone()
    conn.close()

    if not row:
        raise HTTPException(404, "Product not found")

    currency = cur["value"] if cur else "KES"
    px_w = int(width_mm * 3.78)
    px_h = int(height_mm * 3.78)

    try:
        png = compose_label_image(
            dict(row), barcode_type, px_w, px_h,
            show_name=True, show_price=True, show_sku=True, currency=currency
        )
        return Response(content=png, media_type="image/png",
                        headers={"Cache-Control": "no-cache"})
    except RuntimeError as e:
        raise HTTPException(503, str(e))
