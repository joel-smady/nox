"""
Neural POS — M-Pesa Integration (Daraja STK Push)
- Reads credentials from .env
- Real Daraja API when credentials are configured
- Simulation fallback when in sandbox/demo mode
- Callback endpoint for Safaricom post-payment notification
"""
from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel
from typing import Optional
from models.database import get_db
from routers.deps import get_current_user
import os, random, string, datetime, logging

try:
    import httpx
    HTTPX_OK = True
except ImportError:
    HTTPX_OK = False

logger = logging.getLogger("neural_pos.mpesa")
router = APIRouter()

# ── Load credentials from .env ────────────────────────────────────────────────
def _cfg():
    return {
        "business_shortcode": os.getenv("MPESA_SHORTCODE", "174379"),
        "passkey":            os.getenv("MPESA_PASSKEY", ""),
        "consumer_key":       os.getenv("MPESA_CONSUMER_KEY", ""),
        "consumer_secret":    os.getenv("MPESA_CONSUMER_SECRET", ""),
        "callback_url":       os.getenv("MPESA_CALLBACK_URL", ""),
        "environment":        os.getenv("MPESA_ENV", "sandbox"),
    }

def _is_live():
    c = _cfg()
    return (c["environment"] == "production" and
            c["consumer_key"] and c["consumer_key"] != "YOUR_CONSUMER_KEY" and
            c["passkey"] and c["passkey"] != "YOUR_DARAJA_PASSKEY" and
            HTTPX_OK)

def _base_url():
    env = _cfg()["environment"]
    return ("https://api.safaricom.co.ke" if env == "production"
            else "https://sandbox.safaricom.co.ke")

# ── Phone normaliser ──────────────────────────────────────────────────────────
def normalize_phone(phone: str) -> str:
    p = phone.strip().replace(" ", "").replace("-", "")
    if p.startswith("+254"): return p[1:]
    if p.startswith("0"):    return "254" + p[1:]
    if p.startswith("254"):  return p
    return "254" + p

def generate_checkout_id():
    return "ws_CO_" + ''.join(random.choices(string.digits, k=18))

def generate_merchant_id():
    return ''.join(random.choices(string.digits, k=10))

# ── Real Daraja STK Push ──────────────────────────────────────────────────────
async def _daraja_stk_push(phone: str, amount: float, order_ref: str, account_ref: str) -> dict:
    import base64
    cfg        = _cfg()
    timestamp  = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    shortcode  = cfg["business_shortcode"]
    passkey    = cfg["passkey"]
    password   = base64.b64encode(f"{shortcode}{passkey}{timestamp}".encode()).decode()
    base_url   = _base_url()

    async with httpx.AsyncClient(timeout=30.0) as client:
        # 1. OAuth token
        auth_str = base64.b64encode(
            f"{cfg['consumer_key']}:{cfg['consumer_secret']}".encode()
        ).decode()
        token_res = await client.get(
            f"{base_url}/oauth/v1/generate?grant_type=client_credentials",
            headers={"Authorization": f"Basic {auth_str}"}
        )
        token_res.raise_for_status()
        token = token_res.json()["access_token"]

        # 2. STK Push
        stk_res = await client.post(
            f"{base_url}/mpesa/stkpush/v1/processrequest",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "BusinessShortCode": shortcode,
                "Password":          password,
                "Timestamp":         timestamp,
                "TransactionType":   "CustomerPayBillOnline",
                "Amount":            int(amount),
                "PartyA":            phone,
                "PartyB":            shortcode,
                "PhoneNumber":       phone,
                "CallBackURL":       cfg["callback_url"],
                "AccountReference":  account_ref,
                "TransactionDesc":   order_ref[:20],
            }
        )
        stk_res.raise_for_status()
        data = stk_res.json()
        if data.get("ResponseCode") != "0":
            raise ValueError(data.get("ResponseDescription", "STK push rejected"))
        return {
            "checkout_request_id": data["CheckoutRequestID"],
            "merchant_request_id": data["MerchantRequestID"],
        }

# ── Pydantic models ───────────────────────────────────────────────────────────
class STKRequest(BaseModel):
    phone:       str
    amount:      float
    order_ref:   str
    account_ref: Optional[str] = "NeuralPOS"

# ── Routes ────────────────────────────────────────────────────────────────────
@router.post("/stk-push")
async def initiate_stk_push(data: STKRequest, current_user=Depends(get_current_user)):
    phone = normalize_phone(data.phone)
    if len(phone) != 12:
        raise HTTPException(400, f"Invalid phone number: {data.phone}. Use format 0712345678")
    if data.amount <= 0:
        raise HTTPException(400, "Amount must be greater than 0")

    cfg = _cfg()

    if _is_live():
        # Real Daraja
        try:
            result = await _daraja_stk_push(
                phone, data.amount, data.order_ref, data.account_ref or "NeuralPOS"
            )
            checkout_id = result["checkout_request_id"]
            merchant_id = result["merchant_request_id"]
            mode = "production"
        except Exception as e:
            logger.error(f"Daraja STK push failed: {e}")
            raise HTTPException(502, f"M-Pesa request failed: {e}")
    else:
        # Simulation
        checkout_id = generate_checkout_id()
        merchant_id = generate_merchant_id()
        mode = cfg["environment"]

    db = get_db()
    db.execute("""
        INSERT INTO mpesa_transactions
        (checkout_request_id, merchant_request_id, phone, amount, order_ref, account_ref, status)
        VALUES (?, ?, ?, ?, ?, ?, 'pending')
    """, (checkout_id, merchant_id, phone, data.amount, data.order_ref, data.account_ref or "NeuralPOS"))
    db.commit(); db.close()

    return {
        "success":              True,
        "checkout_request_id":  checkout_id,
        "merchant_request_id":  merchant_id,
        "customer_message":     f"A payment request of KES {data.amount:,.0f} has been sent to {data.phone}. Please enter your M-Pesa PIN.",
        "phone":                data.phone,
        "amount":               data.amount,
        "mode":                 mode,
        "live":                 _is_live(),
    }

@router.get("/status/{checkout_request_id}")
async def check_payment_status(checkout_request_id: str, current_user=Depends(get_current_user)):
    db = get_db()
    row = db.execute("""
        SELECT checkout_request_id, phone, amount, order_ref, status, mpesa_receipt, result_desc, created_at
        FROM mpesa_transactions WHERE checkout_request_id=?
    """, (checkout_request_id,)).fetchone()
    db.close()
    if not row:
        raise HTTPException(404, "Transaction not found")
    return dict(zip(
        ["checkout_request_id","phone","amount","order_ref","status","mpesa_receipt","result_desc","created_at"],
        row
    ))

@router.post("/simulate-confirm/{checkout_request_id}")
async def simulate_confirmation(checkout_request_id: str, current_user=Depends(get_current_user)):
    """SANDBOX ONLY: manually confirm a pending STK push."""
    db = get_db()
    txn = db.execute("SELECT id, status FROM mpesa_transactions WHERE checkout_request_id=?",
                     (checkout_request_id,)).fetchone()
    if not txn:
        db.close(); raise HTTPException(404, "Transaction not found")
    if txn[1] != "pending":
        db.close(); raise HTTPException(400, f"Transaction already {txn[1]}")

    receipt = "NLJ" + ''.join(random.choices(string.ascii_uppercase + string.digits, k=7))
    db.execute("""
        UPDATE mpesa_transactions
        SET status='confirmed', mpesa_receipt=?,
            result_desc='The service request is processed successfully.',
            updated_at=datetime('now')
        WHERE checkout_request_id=?
    """, (receipt, checkout_request_id))
    db.commit(); db.close()
    return {"status": "confirmed", "mpesa_receipt": receipt}

@router.post("/simulate-fail/{checkout_request_id}")
async def simulate_failure(checkout_request_id: str, current_user=Depends(get_current_user)):
    """SANDBOX: simulate user cancelling payment."""
    db = get_db()
    db.execute("""
        UPDATE mpesa_transactions SET status='failed',
        result_desc='The transaction was cancelled by the user.',
        updated_at=datetime('now')
        WHERE checkout_request_id=?
    """, (checkout_request_id,))
    db.commit(); db.close()
    return {"status": "failed", "message": "Payment cancelled"}

@router.post("/callback")
async def mpesa_callback(data: dict):
    """
    Production: Safaricom calls this after customer completes/cancels payment.
    Register your public URL + /api/mpesa/callback in your Daraja app.
    """
    try:
        body     = data.get("Body", {})
        stk      = body.get("stkCallback", {})
        chk_id   = stk.get("CheckoutRequestID")
        res_code = stk.get("ResultCode")
        res_desc = stk.get("ResultDesc", "")
        if not chk_id:
            return {"ResultCode": 0, "ResultDesc": "Accepted"}
        db = get_db()
        if res_code == 0:
            items   = stk.get("CallbackMetadata", {}).get("Item", [])
            receipt = next((i["Value"] for i in items if i.get("Name") == "MpesaReceiptNumber"), None)
            db.execute("""
                UPDATE mpesa_transactions SET status='confirmed', mpesa_receipt=?,
                result_desc=?, updated_at=datetime('now') WHERE checkout_request_id=?
            """, (receipt, res_desc, chk_id))
        else:
            db.execute("""
                UPDATE mpesa_transactions SET status='failed',
                result_desc=?, updated_at=datetime('now') WHERE checkout_request_id=?
            """, (res_desc, chk_id))
        db.commit(); db.close()
    except Exception as e:
        logger.error(f"M-Pesa callback error: {e}")
    return {"ResultCode": 0, "ResultDesc": "Accepted"}

@router.get("/config-status")
async def mpesa_config_status(current_user=Depends(get_current_user)):
    """Returns whether live M-Pesa is configured or running in demo/sandbox mode."""
    cfg = _cfg()
    return {
        "live":        _is_live(),
        "environment": cfg["environment"],
        "shortcode":   cfg["business_shortcode"],
        "callback_configured": bool(cfg.get("callback_url")),
        "credentials_set": bool(cfg.get("consumer_key") and cfg["consumer_key"] != "YOUR_CONSUMER_KEY"),
    }

@router.get("/transactions")
async def list_transactions(current_user=Depends(get_current_user)):
    db = get_db()
    rows = db.execute("""
        SELECT checkout_request_id, phone, amount, order_ref, status, mpesa_receipt, created_at
        FROM mpesa_transactions ORDER BY created_at DESC LIMIT 100
    """).fetchall()
    db.close()
    keys = ["checkout_request_id","phone","amount","order_ref","status","mpesa_receipt","created_at"]
    return [dict(zip(keys, r)) for r in rows]
