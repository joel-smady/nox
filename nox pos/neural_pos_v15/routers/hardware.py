"""
Neural POS — Hardware Integration Router
Handles: Thermal receipt printer (ESC/POS), cash drawer, barcode scanner config

Supported printer connections:
  - USB:     usb://VendorID/ProductID  (e.g. usb://0x04b8/0x0202)
  - Serial:  /dev/ttyUSB0 or COM3
  - Network: 192.168.1.100:9100
  - Windows: \\\\server\\PrinterShare

Dependencies (add to requirements.txt):
  python-escpos>=3.0
  pyserial
  pyusb        (for USB printers on Linux/Mac)
  pywin32      (Windows only, for network/shared printers)
"""

import os
import json
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from models.database import get_db
from routers.auth import get_current_user

def _model_to_dict(model) -> dict:
    """Pydantic v1/v2 compatible model serialization."""
    if hasattr(model, 'model_dump'):
        return model.model_dump()
    return model.dict()


logger = logging.getLogger("neural_pos.hardware")
router = APIRouter()


# ── Pydantic models ───────────────────────────────────────────────────────────

class PrinterConfig(BaseModel):
    connection_type: str          # "usb" | "serial" | "network" | "windows"
    # USB
    vendor_id: Optional[str] = None    # hex string e.g. "0x04b8"
    product_id: Optional[str] = None   # hex string e.g. "0x0202"
    # Serial
    serial_port: Optional[str] = None  # "/dev/ttyUSB0" or "COM3"
    baud_rate: Optional[int] = 9600
    # Network
    ip_address: Optional[str] = None
    port: Optional[int] = 9100
    # Windows shared printer
    printer_name: Optional[str] = None
    # Receipt layout
    paper_width: Optional[int] = 48    # characters per line (48 for 80mm, 32 for 58mm)
    store_name: Optional[str] = None
    store_address: Optional[str] = None
    store_phone: Optional[str] = None
    receipt_footer: Optional[str] = "Thank you for shopping with us!"
    print_logo: Optional[bool] = False
    open_drawer_on_cash: Optional[bool] = True

class DrawerTriggerRequest(BaseModel):
    order_id: Optional[int] = None    # optional, for logging

class TestPrintRequest(BaseModel):
    message: Optional[str] = "Neural POS — Printer Test OK"


# ── DB helpers ────────────────────────────────────────────────────────────────

def ensure_hardware_table():
    """Create hardware_config table if it doesn't exist."""
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hardware_config (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            key     TEXT UNIQUE NOT NULL,
            value   TEXT,
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    conn.close()

ensure_hardware_table()


def get_hw_config() -> dict:
    """Load full hardware config from DB as a dict."""
    conn = get_db()
    rows = conn.execute("SELECT key, value FROM hardware_config").fetchall()
    conn.close()
    return {r["key"]: r["value"] for r in rows}


def set_hw_config(updates: dict):
    """Upsert hardware config keys into DB."""
    conn = get_db()
    for key, value in updates.items():
        conn.execute("""
            INSERT INTO hardware_config (key, value, updated_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
        """, (key, str(value) if value is not None else None))
    conn.commit()
    conn.close()


# ── Printer factory ───────────────────────────────────────────────────────────

def get_printer():
    """
    Build and return an escpos Printer instance from saved config.
    Raises RuntimeError if config is missing or connection fails.
    """
    try:
        from escpos import printer as escprinter
    except ImportError:
        raise RuntimeError(
            "python-escpos is not installed. "
            "Run: pip install python-escpos pyserial pyusb"
        )

    cfg = get_hw_config()
    conn_type = cfg.get("connection_type", "").strip()

    if not conn_type:
        raise RuntimeError("Printer not configured. Please set up printer in Hardware Settings.")

    if conn_type == "network":
        ip = cfg.get("ip_address")
        port = int(cfg.get("port", 9100))
        if not ip:
            raise RuntimeError("Network printer IP address not configured.")
        return escprinter.Network(ip, port=port)

    elif conn_type == "usb":
        vid = cfg.get("vendor_id")
        pid = cfg.get("product_id")
        if not vid or not pid:
            raise RuntimeError("USB printer Vendor ID / Product ID not configured.")
        # Accept hex strings like "0x04b8" or plain ints
        vendor_id  = int(vid, 16) if isinstance(vid, str) and vid.startswith("0x") else int(vid)
        product_id = int(pid, 16) if isinstance(pid, str) and pid.startswith("0x") else int(pid)
        return escprinter.Usb(vendor_id, product_id)

    elif conn_type == "serial":
        port_path = cfg.get("serial_port")
        baud = int(cfg.get("baud_rate", 9600))
        if not port_path:
            raise RuntimeError("Serial printer port not configured.")
        return escprinter.Serial(port_path, baudrate=baud)

    elif conn_type == "windows":
        printer_name = cfg.get("printer_name")
        if not printer_name:
            raise RuntimeError("Windows printer name not configured.")
        try:
            return escprinter.Win32Raw(printer_name)
        except AttributeError:
            raise RuntimeError(
                "Windows shared printer is not supported on Linux. "
                "Use Network (IP) or USB connection instead."
            )

    else:
        raise RuntimeError(f"Unknown connection type: '{conn_type}'")


# ── Receipt builder ───────────────────────────────────────────────────────────

def build_and_print_receipt(order: dict, items: list, cfg: dict):
    """
    Format and print a receipt using ESC/POS commands.
    order : dict from the orders table
    items : list of dicts from order_items table
    cfg   : hardware_config dict
    """
    p = get_printer()
    width = int(cfg.get("paper_width", 48))
    store_name    = cfg.get("store_name",    "Neural POS")
    store_address = cfg.get("store_address", "")
    store_phone   = cfg.get("store_phone",   "")
    footer        = cfg.get("receipt_footer", "Thank you for shopping with us!")

    def divider(char="-"):
        return char * width

    # ── Header ────────────────────────────────────────────────────────────────
    p.set(align="center", bold=True, double_height=True, double_width=True)
    p.text(f"{store_name}\n")
    p.set(align="center", bold=False, double_height=False, double_width=False)
    if store_address:
        p.text(f"{store_address}\n")
    if store_phone:
        p.text(f"Tel: {store_phone}\n")
    p.text(f"\n{divider('=')}\n")

    # ── Order meta ────────────────────────────────────────────────────────────
    p.set(align="left")
    ts = order.get("created_at", datetime.now().isoformat())
    # Format: 2026-03-28T14:30:00  →  28/03/2026  14:30
    try:
        dt = datetime.fromisoformat(ts)
        date_str = dt.strftime("%d/%m/%Y  %H:%M")
    except Exception:
        date_str = ts[:16]

    p.text(f"Order : {order.get('order_number', 'N/A')}\n")
    p.text(f"Date  : {date_str}\n")
    if order.get("cashier_name"):
        p.text(f"Cashier: {order['cashier_name']}\n")
    if order.get("customer_name"):
        p.text(f"Client : {order['customer_name']}\n")
    p.text(f"{divider()}\n")

    # ── Column header ─────────────────────────────────────────────────────────
    # Format: "Item Name         Qty  Price   Total"
    col_name  = width - 20
    p.set(bold=True)
    header = f"{'Item':<{col_name}} {'Qty':>3}  {'Price':>7}  {'Total':>7}"
    p.text(f"{header}\n")
    p.set(bold=False)
    p.text(f"{divider()}\n")

    # ── Line items ────────────────────────────────────────────────────────────
    for item in items:
        name  = str(item.get("product_name", ""))
        qty   = int(item.get("quantity", 1))
        price = float(item.get("unit_price", 0))
        total = float(item.get("total_price", 0))

        # Truncate long names to fit column
        if len(name) > col_name:
            name = name[:col_name - 1] + "…"

        line = f"{name:<{col_name}} {qty:>3}  {price:>7,.2f}  {total:>7,.2f}"
        p.text(f"{line}\n")

    p.text(f"{divider()}\n")

    # ── Totals ────────────────────────────────────────────────────────────────
    subtotal = float(order.get("subtotal", 0))
    tax      = float(order.get("tax", 0))
    discount = float(order.get("discount", 0))
    total    = float(order.get("total", 0))

    label_w = width - 12

    p.text(f"{'Subtotal':<{label_w}} KES {subtotal:>8,.2f}\n")
    if discount > 0:
        p.text(f"{'Discount':<{label_w}} KES {discount:>8,.2f}\n")
    p.text(f"{'VAT (16%)':<{label_w}} KES {tax:>8,.2f}\n")
    p.text(f"{divider()}\n")

    p.set(bold=True, double_height=True)
    p.text(f"{'TOTAL':<{label_w}} KES {total:>8,.2f}\n")
    p.set(bold=False, double_height=False)

    # ── Payment method ────────────────────────────────────────────────────────
    method = str(order.get("payment_method", "cash")).upper()
    p.text(f"\nPayment: {method}\n")
    # Print split breakdown if present in notes
    notes = order.get("notes", "") or ""
    if notes.startswith("Split:"):
        p.text(f"{notes}\n")

    # ── Footer ────────────────────────────────────────────────────────────────
    p.text(f"\n{divider('=')}\n")
    p.set(align="center")
    p.text(f"{footer}\n")
    p.text("\n\n\n")  # Feed paper before cut

    # ── Cut ───────────────────────────────────────────────────────────────────
    p.cut()

    # ── Auto-open drawer on cash payments ─────────────────────────────────────
    if cfg.get("open_drawer_on_cash", "True") in (True, "True", "true", "1"):
        payment = order.get("payment_method", "").lower()
        if payment in ("cash", "split"):
            open_cash_drawer_raw(p)

    p.close()


def open_cash_drawer_raw(printer_instance=None):
    """
    Send ESC/POS cash drawer pulse (pin 2 or pin 5).
    Works on most Epson-compatible thermal printers.
    If printer_instance is None, opens a new connection.
    """
    ESC = b'\x1b'
    # ESC p m t1 t2 — drawer kick
    # m=0 → pin 2, m=1 → pin 5 ; t1/t2 = pulse timings
    DRAWER_PIN2 = ESC + b'\x70\x00\x19\x78'  # pin 2, ~25ms on / 120ms off
    DRAWER_PIN5 = ESC + b'\x70\x01\x19\x78'  # pin 5

    own_connection = printer_instance is None
    if own_connection:
        printer_instance = get_printer()

    try:
        printer_instance._raw(DRAWER_PIN2)
    finally:
        if own_connection:
            try:
                printer_instance.close()
            except Exception:
                pass


# ── API endpoints ─────────────────────────────────────────────────────────────

@router.get("/config")
async def get_hardware_config(request: Request):
    """Return current hardware configuration (passwords/keys stripped)."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    if user.get("role") not in ("admin", "manager"):
        raise HTTPException(status_code=403, detail="Admin/Manager access required")

    cfg = get_hw_config()
    # Never expose raw internal fields — just return safe display fields
    return {
        "connection_type":       cfg.get("connection_type"),
        "vendor_id":             cfg.get("vendor_id"),
        "product_id":            cfg.get("product_id"),
        "serial_port":           cfg.get("serial_port"),
        "baud_rate":             cfg.get("baud_rate"),
        "ip_address":            cfg.get("ip_address"),
        "port":                  cfg.get("port"),
        "printer_name":          cfg.get("printer_name"),
        "paper_width":           cfg.get("paper_width", "48"),
        "store_name":            cfg.get("store_name", ""),
        "store_address":         cfg.get("store_address", ""),
        "store_phone":           cfg.get("store_phone", ""),
        "receipt_footer":        cfg.get("receipt_footer", "Thank you for shopping with us!"),
        "print_logo":            cfg.get("print_logo", "false"),
        "open_drawer_on_cash":   cfg.get("open_drawer_on_cash", "true"),
    }


@router.post("/config")
async def save_hardware_config(config: PrinterConfig, request: Request):
    """Save printer / drawer configuration."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    if user.get("role") not in ("admin", "manager"):
        raise HTTPException(status_code=403, detail="Admin/Manager access required")

    updates = {k: v for k, v in _model_to_dict(config).items() if v is not None}
    set_hw_config(updates)
    logger.info(f"Hardware config updated by {user.get('username')}")
    return {"success": True, "message": "Hardware configuration saved."}


@router.post("/print-receipt/{order_id}")
async def print_receipt(order_id: int, request: Request):
    """
    Print a receipt for an existing order.
    Called automatically after checkout or manually from order history.
    """
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")

    conn = get_db()
    cursor = conn.cursor()

    # Fetch order with cashier name
    cursor.execute("""
        SELECT o.*, u.full_name as cashier_name
        FROM orders o
        LEFT JOIN users u ON o.cashier_id = u.id
        WHERE o.id = ?
    """, (order_id,))
    order = cursor.fetchone()
    if not order:
        conn.close()
        raise HTTPException(status_code=404, detail="Order not found")

    cursor.execute("SELECT * FROM order_items WHERE order_id = ?", (order_id,))
    items = [dict(row) for row in cursor.fetchall()]
    conn.close()

    cfg = get_hw_config()

    try:
        build_and_print_receipt(dict(order), items, cfg)
        logger.info(f"Receipt printed for order {order_id} by {user.get('username')}")
        return {"success": True, "message": f"Receipt printed for order #{order_id}"}
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.error(f"Print error for order {order_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Print failed: {str(e)}")


@router.post("/open-drawer")
async def open_cash_drawer(body: DrawerTriggerRequest, request: Request):
    """
    Manually open the cash drawer.
    Requires manager/admin or cashier role (cashiers need it for change).
    """
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")

    try:
        open_cash_drawer_raw()
        logger.info(
            f"Cash drawer opened manually by {user.get('username')}"
            + (f" (order {body.order_id})" if body.order_id else "")
        )
        return {"success": True, "message": "Cash drawer opened."}
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.error(f"Drawer error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Drawer open failed: {str(e)}")


@router.post("/test-print")
async def test_print(body: TestPrintRequest, request: Request):
    """Print a test page to verify printer connectivity."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    if user.get("role") not in ("admin", "manager"):
        raise HTTPException(status_code=403, detail="Admin/Manager access required")

    try:
        from escpos import printer as escprinter
    except ImportError:
        raise HTTPException(
            status_code=503,
            detail="python-escpos not installed. Run: pip install python-escpos pyserial pyusb"
        )

    try:
        p = get_printer()
        cfg = get_hw_config()
        width = int(cfg.get("paper_width", 48))

        p.set(align="center", bold=True)
        p.text("=== PRINTER TEST ===\n")
        p.set(bold=False)
        p.text(f"{body.message}\n")
        p.text(f"Time: {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}\n")
        p.text("-" * width + "\n")
        p.text("Normal text\n")
        p.set(bold=True)
        p.text("Bold text\n")
        p.set(bold=False, double_height=True)
        p.text("Double height\n")
        p.set(double_height=False, underline=True)
        p.text("Underline text\n")
        p.set(underline=False, align="center")
        p.text("=== TEST COMPLETE ===\n")
        p.text("\n\n\n")
        p.cut()
        p.close()

        return {"success": True, "message": "Test page printed successfully."}
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.error(f"Test print error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Test print failed: {str(e)}")


@router.get("/status")
async def hardware_status(request: Request):
    """
    Check hardware status — verifies printer config exists and
    attempts a brief connection test without printing anything.
    """
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")

    cfg = get_hw_config()
    conn_type = cfg.get("connection_type")

    status = {
        "printer_configured": bool(conn_type),
        "connection_type": conn_type,
        "drawer_configured": bool(conn_type),   # drawer uses same connection
        "scanner_note": "USB HID barcode scanners work plug-and-play. "
                        "Point the scanner at the product search field to scan.",
    }

    if conn_type:
        try:
            p = get_printer()
            p.close()
            status["printer_reachable"] = True
            status["printer_message"]   = "Printer connection OK"
        except Exception as e:
            status["printer_reachable"] = False
            status["printer_message"]   = str(e)
    else:
        status["printer_reachable"] = False
        status["printer_message"]   = "No printer configured"

    return status


@router.get("/scanner/config")
async def get_scanner_config(request: Request):
    """
    Return barcode scanner guidance.
    USB HID scanners need zero driver install — they emulate a keyboard.
    This endpoint documents the setup for the frontend.
    """
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")

    return {
        "type": "USB HID (keyboard emulation)",
        "setup": [
            "Plug the USB barcode scanner into any USB port.",
            "In the POS screen, click the product search box.",
            "Scan a barcode — it types the barcode and presses Enter automatically.",
            "The product will be looked up and added to the cart.",
        ],
        "supported_formats": ["EAN-13", "EAN-8", "UPC-A", "UPC-E", "Code 128", "Code 39", "QR Code"],
        "troubleshooting": [
            "If scan does nothing: ensure the search box is focused (clicked).",
            "If wrong characters appear: check scanner is in USB-HID mode (see scanner manual).",
            "For serial/RS-232 scanners: use a USB-to-Serial adapter and configure via Serial port settings.",
        ]
    }
