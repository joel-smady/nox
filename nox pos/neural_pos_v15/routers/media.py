"""
Neural POS — Media Router
Handles product image upload, storage, and serving.

Images are stored in static/img/products/ as JPEG (resized to max 600x600).
Filenames: product_{id}.jpg

Dependencies:
  Pillow  — image resizing/conversion
"""

import os
import io
import logging
from fastapi import APIRouter, HTTPException, Request, UploadFile, File
from fastapi.responses import FileResponse, Response

from models.database import get_db
from routers.auth import get_current_user

logger = logging.getLogger("neural_pos.media")
router = APIRouter()

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMG_DIR     = os.path.join(_ROOT, "static", "img", "products")
MAX_SIZE_KB = 2048      # 2MB upload limit
THUMB_SIZE  = (600, 600)
PLACEHOLDER = os.path.join(_ROOT, "static", "img", "placeholder.svg")

os.makedirs(IMG_DIR, exist_ok=True)

# Create a simple SVG placeholder if it doesn't exist
if not os.path.exists(PLACEHOLDER):
    os.makedirs(os.path.join(_ROOT, "static", "img"), exist_ok=True)
    with open(PLACEHOLDER, "w") as f:
        f.write("""<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200" viewBox="0 0 200 200">
  <rect width="200" height="200" fill="#111122" rx="8"/>
  <rect x="60" y="60" width="80" height="60" rx="4" fill="none" stroke="#2a2a55" stroke-width="2"/>
  <circle cx="80" cy="80" r="8" fill="#2a2a55"/>
  <polyline points="60,120 85,95 100,110 120,85 140,120" fill="none" stroke="#2a2a55" stroke-width="2"/>
  <text x="100" y="155" font-family="monospace" font-size="11" fill="#7a7468" text-anchor="middle">No Image</text>
</svg>""")


def get_product_img_path(product_id: int) -> str:
    return os.path.join(IMG_DIR, f"product_{product_id}.jpg")


def img_exists(product_id: int) -> bool:
    return os.path.exists(get_product_img_path(product_id))


@router.post("/products/{product_id}/image")
async def upload_product_image(product_id: int, request: Request, file: UploadFile = File(...)):
    """
    Upload a product image. Accepts JPEG, PNG, GIF, WEBP.
    Resizes to max 600×600 px and saves as JPEG.
    """
    user = get_current_user(request)
    if not user or user.get("role") not in ("admin", "manager"):
        raise HTTPException(403, "Manager/Admin required")

    # Verify product exists
    conn = get_db()
    row = conn.execute("SELECT id FROM products WHERE id = ?", (product_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "Product not found")

    # Check content type
    allowed = {"image/jpeg", "image/png", "image/gif", "image/webp"}
    ct = file.content_type or ""
    if ct not in allowed:
        raise HTTPException(400, f"Unsupported file type: {ct}. Use JPEG, PNG, GIF, or WEBP.")

    # Read and size-check
    data = await file.read()
    if len(data) > MAX_SIZE_KB * 1024:
        raise HTTPException(400, f"File too large. Max {MAX_SIZE_KB}KB.")

    # Resize with Pillow
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(data)).convert("RGB")
        img.thumbnail(THUMB_SIZE, Image.LANCZOS)
        out_path = get_product_img_path(product_id)
        img.save(out_path, "JPEG", quality=85, optimize=True)
        logger.info(f"Image saved for product {product_id}: {out_path} ({img.size})")
    except ImportError:
        raise HTTPException(503, "Pillow not installed. Run: pip install Pillow")
    except Exception as e:
        logger.error(f"Image processing error for product {product_id}: {e}")
        raise HTTPException(500, f"Image processing failed: {e}")

    return {
        "success":  True,
        "url":      f"/api/media/products/{product_id}/image",
        "message":  "Image uploaded successfully",
    }


@router.get("/products/{product_id}/image")
async def get_product_image(product_id: int):
    """Serve a product image. Returns placeholder SVG if no image exists."""
    path = get_product_img_path(product_id)
    if os.path.exists(path):
        return FileResponse(path, media_type="image/jpeg",
                            headers={"Cache-Control": "public, max-age=86400"})
    # Return placeholder
    if os.path.exists(PLACEHOLDER):
        return FileResponse(PLACEHOLDER, media_type="image/svg+xml",
                            headers={"Cache-Control": "public, max-age=3600"})
    raise HTTPException(404, "No image found")


@router.delete("/products/{product_id}/image")
async def delete_product_image(product_id: int, request: Request):
    """Delete a product's image."""
    user = get_current_user(request)
    if not user or user.get("role") not in ("admin", "manager"):
        raise HTTPException(403, "Manager/Admin required")

    path = get_product_img_path(product_id)
    if os.path.exists(path):
        os.remove(path)
        return {"success": True, "message": "Image deleted"}
    raise HTTPException(404, "No image to delete")


@router.get("/products/images/list")
async def list_products_with_images(request: Request):
    """Return list of product IDs that have images uploaded."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(401, "Authentication required")

    ids = []
    if os.path.exists(IMG_DIR):
        for fname in os.listdir(IMG_DIR):
            if fname.startswith("product_") and fname.endswith(".jpg"):
                try:
                    pid = int(fname.replace("product_", "").replace(".jpg", ""))
                    ids.append(pid)
                except ValueError:
                    pass
    return {"product_ids_with_images": ids}
