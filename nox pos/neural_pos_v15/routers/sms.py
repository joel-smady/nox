"""
Neural POS — SMS / WhatsApp Receipts
Africa's Talking integration for sending receipts via SMS or WhatsApp.
In sandbox mode, logs receipts instead of sending. Set AT_API_KEY to go live.
"""
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional
from models.database import get_db
from routers.auth import get_current_user
import datetime

router = APIRouter()

# ── Africa's Talking config ───────────────────────────────────────────────────
AT_CONFIG = {
    "username": "sandbox",           # Change to your AT username for live
    "api_key":  "YOUR_AT_API_KEY",   # Get from africastalking.com
    "sender_id": "NeuralPOS",        # Must be registered with AT
    "environment": "sandbox"         # Change to "production" for live
}

class ReceiptRequest(BaseModel):
    phone: str
    order_number: str
    items: list
    subtotal: float
    tax: float
    total: float
    payment_method: str
    channel: Optional[str] = "sms"   # "sms" or "whatsapp"


def build_receipt_sms(data: ReceiptRequest) -> str:
    """Build a short SMS receipt (160 chars ideal)"""
    items_short = ", ".join([
        f"{i.get('product_name', i.get('name','?'))} x{i.get('quantity',1)}"
        for i in (data.items or [])[:3]
    ])
    if len(data.items or []) > 3:
        items_short += f" +{len(data.items)-3} more"
    method = data.payment_method.replace("_"," ").upper()
    return (
        f"NeuralPOS Receipt\n"
        f"Order: {data.order_number}\n"
        f"{items_short}\n"
        f"Total: KES {data.total:,.0f} ({method})\n"
        f"Thank you!"
    )

def build_receipt_whatsapp(data: ReceiptRequest) -> str:
    """Richer WhatsApp receipt"""
    lines = [f"*NeuralPOS Receipt* 🧾", f"Order: *{data.order_number}*", ""]
    for item in (data.items or []):
        name = item.get('product_name', item.get('name','?'))
        qty  = item.get('quantity',1)
        price= item.get('total_price', item.get('unit_price',0) * qty)
        lines.append(f"• {name} ×{qty}  KES {price:,.0f}")
    lines += [
        "",
        f"Subtotal: KES {data.subtotal:,.0f}",
        f"VAT (16%): KES {data.tax:,.0f}",
        f"*Total: KES {data.total:,.0f}*",
        f"Payment: {data.payment_method.replace('_',' ').title()}",
        "",
        "Thank you for your business! 🙏"
    ]
    return "\n".join(lines)

async def _send_via_at(phone: str, message: str, channel: str) -> dict:
    """
    PRODUCTION: Uncomment and use when AT_CONFIG has real credentials.

    try:
        import httpx
        auth = (AT_CONFIG["username"], AT_CONFIG["api_key"])
        if channel == "whatsapp":
            url = "https://chat.africastalking.com/whatsapp/message"
            payload = {"username": AT_CONFIG["username"], "to": phone, "message": message}
        else:
            url = "https://api.africastalking.com/version1/messaging"
            payload = {"username": AT_CONFIG["username"], "to": phone,
                       "message": message, "from": AT_CONFIG["sender_id"]}
        async with httpx.AsyncClient(timeout=10) as client:
            res = await client.post(url, data=payload,
                                    headers={"apikey": AT_CONFIG["api_key"],
                                             "Accept": "application/json"})
            return res.json()
    except Exception as e:
        return {"error": str(e)}
    """
    # SANDBOX: Just log it
    return {"status": "sandbox", "message": "SMS logged (sandbox mode — add AT API key to go live)"}

def normalize_phone(phone: str) -> str:
    p = phone.strip().replace(" ","").replace("-","")
    if p.startswith("+254"): return p
    if p.startswith("0"):    return "+254" + p[1:]
    if p.startswith("254"):  return "+" + p
    return "+" + p

@router.post("/send-receipt")
async def send_receipt(data: ReceiptRequest, current_user=Depends(get_current_user)):
    phone = normalize_phone(data.phone)
    if data.channel == "whatsapp":
        message = build_receipt_whatsapp(data)
    else:
        message = build_receipt_sms(data)

    result = await _send_via_at(phone, message, data.channel)

    # Log it
    db = get_db()
    db.execute(
        "INSERT INTO sms_log (phone,message,channel,order_ref,status,provider_ref) VALUES (?,?,?,?,?,?)",
        (phone, message, data.channel, data.order_number,
         result.get("status","sent"), str(result.get("messageId","")))
    )
    db.commit(); db.close()

    return {
        "success": True,
        "phone": phone,
        "channel": data.channel,
        "message_preview": message[:100] + "..." if len(message) > 100 else message,
        "provider_response": result,
        "note": "Add Africa's Talking API key in routers/sms.py to send real messages"
    }

@router.get("/log")
async def sms_log(current_user=Depends(get_current_user)):
    db = get_db()
    rows = db.execute(
        "SELECT id,phone,channel,order_ref,status,created_at FROM sms_log ORDER BY created_at DESC LIMIT 50"
    ).fetchall()
    db.close()
    return [dict(zip(["id","phone","channel","order_ref","status","created_at"], r)) for r in rows]

@router.get("/status")
async def sms_status():
    is_live = AT_CONFIG["api_key"] != "YOUR_AT_API_KEY"
    return {
        "mode": "live" if is_live else "sandbox",
        "provider": "Africa's Talking",
        "channels": ["sms", "whatsapp"],
        "setup": "Visit africastalking.com → get API key → update AT_CONFIG in routers/sms.py"
    }
