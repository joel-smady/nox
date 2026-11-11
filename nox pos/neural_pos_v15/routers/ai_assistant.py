"""
Neural POS — AI Assistant (v9 rebuilt)
Fixes:
  - Auto-detects any Ollama model (not hardcoded to llama3.1)
  - SSE streaming so responses appear word-by-word
  - Multi-turn conversation history (memory within session)
  - /api/ai/models endpoint for UI model picker
  - ai_conversations table ensured in schema
  - Page-aware system context (inventory page → stock focus, etc.)
  - Generous timeout (120s) for large models
  - Actionable setup guidance when offline
"""
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Optional, List
import json, os
from datetime import datetime
from models.database import get_db
from routers.deps import get_current_user

try:
    import httpx
    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False

router = APIRouter()

OLLAMA_URL      = os.getenv("OLLAMA_URL", "http://localhost:11434")
DEFAULT_MODEL   = os.getenv("AI_MODEL", "")   # empty = auto-detect first available
TIMEOUT_CONNECT = 5.0
TIMEOUT_READ    = 120.0   # large models need time

# ── DB setup ──────────────────────────────────────────────────────────────────

# ── Models ────────────────────────────────────────────────────────────────────
class AIMessage(BaseModel):
    message:    str
    context:    Optional[str] = "general"
    model:      Optional[str] = ""         # empty = use server default
    session_id: Optional[str] = ""
    history:    Optional[List[dict]] = None  # [{role, content}, ...]

# ── Ollama helpers ────────────────────────────────────────────────────────────
async def get_available_models() -> list:
    """Return list of model names available in this Ollama instance."""
    if not HTTPX_AVAILABLE:
        return []
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_CONNECT) as client:
            res = await client.get(f"{OLLAMA_URL}/api/tags")
            if res.status_code == 200:
                return [m.get("name", "") for m in res.json().get("models", [])]
    except Exception:
        pass
    return []

async def pick_model(requested: str = "") -> str:
    """Return the model to use — requested > env default > first available."""
    if requested:
        return requested
    if DEFAULT_MODEL:
        return DEFAULT_MODEL
    models = await get_available_models()
    return models[0] if models else "llama3.1"

def get_system_context(conn, page_context: str = "general") -> str:
    """Build a rich system prompt with live POS data, focused on the current page."""
    cur = conn.cursor()
    today_row = cur.execute(
        "SELECT COUNT(*), COALESCE(SUM(total),0) FROM orders WHERE DATE(created_at)=DATE('now') AND status='completed'"
    ).fetchone()
    week_rev = cur.execute(
        "SELECT COALESCE(SUM(total),0) FROM orders WHERE DATE(created_at)>=DATE('now','-7 days') AND status='completed'"
    ).fetchone()[0]
    low_stock = cur.execute(
        "SELECT name, stock_quantity, reorder_level FROM products WHERE stock_quantity<=reorder_level AND is_active=1 LIMIT 8"
    ).fetchall()
    top_today = cur.execute(
        """SELECT oi.product_name, SUM(oi.quantity) qty
           FROM order_items oi JOIN orders o ON oi.order_id=o.id
           WHERE DATE(o.created_at)=DATE('now') GROUP BY oi.product_name ORDER BY qty DESC LIMIT 5"""
    ).fetchall()
    credit_out = cur.execute(
        "SELECT COALESCE(SUM(balance),0) FROM credit_sales WHERE status IN ('unpaid','overdue')"
    ).fetchone()[0]
    expenses_today = cur.execute(
        "SELECT COALESCE(SUM(amount),0) FROM expenses WHERE date=DATE('now')"
    ).fetchone()[0]

    low_list  = ", ".join([f"{r[0]} ({r[1]}/{r[2]})" for r in low_stock]) or "None"
    top_list  = ", ".join([f"{r[0]} (×{r[1]})" for r in top_today]) or "None yet"

    # Page-specific focus instructions
    page_focus = {
        "pos":           "The user is at the POS terminal. Focus on helping complete the current sale, suggest upsells, or answer quick product questions.",
        "inventory":     "The user is managing inventory. Focus on stock levels, reorder suggestions, expiry, and supplier recommendations.",
        "analytics":     "The user is reviewing analytics. Provide data insights, trends, and actionable business recommendations.",
        "suppliers":     "The user is managing suppliers. Help with purchase order decisions, supplier comparisons, and reorder quantities.",
        "customers":     "The user is managing customers. Help with loyalty, credit limits, and customer retention strategies.",
        "expenses":      "The user is reviewing expenses. Help identify cost reduction opportunities and unusual spending patterns.",
        "credit":        "The user is managing credit sales. Help assess risk, suggest follow-up actions for overdue accounts.",
        "eod":           "The user is doing end-of-day cash-up. Help reconcile, explain variances, and summarise the day.",
        "reports":       "The user is reviewing reports. Help interpret the numbers and suggest improvements.",
        "staff":         "The user is managing staff. Help with performance analysis and scheduling.",
    }.get(page_context, "Help with any aspect of the business.")

    return f"""You are Neural, the AI assistant built into Neural POS — a professional Kenyan point-of-sale system.

LIVE BUSINESS DATA (right now):
• Today: {today_row[0]} orders · KES {today_row[1]:,.0f} revenue
• This week revenue: KES {week_rev:,.0f}
• Today's expenses: KES {expenses_today:,.0f}
• Top products today: {top_list}
• Low/out-of-stock items: {low_list}
• Outstanding credit (deni): KES {credit_out:,.0f}
• Date/Time: {datetime.now().strftime('%A, %d %B %Y — %H:%M')}

CURRENT CONTEXT: {page_focus}

RULES:
- Be concise — aim for under 150 words unless a detailed analysis is explicitly requested
- Always use KES for currency amounts
- When recommending actions, be specific (name actual products, amounts, suppliers)
- If you don't have enough data to answer confidently, say so
- Respond in the same language the user writes in (Swahili or English)"""

# ── Streaming chat ────────────────────────────────────────────────────────────
async def stream_ollama(model: str, messages: list):
    """Stream tokens from Ollama as SSE events."""
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(connect=TIMEOUT_CONNECT, read=TIMEOUT_READ, write=10, pool=10)
    ) as client:
        async with client.stream("POST", f"{OLLAMA_URL}/api/chat", json={
            "model":    model,
            "messages": messages,
            "stream":   True,
            "options":  {"temperature": 0.7, "num_predict": 600}
        }) as res:
            if res.status_code != 200:
                yield f"data: {json.dumps({'error': f'Ollama error {res.status_code}'})}\n\n"
                return
            async for line in res.aiter_lines():
                if not line.strip():
                    continue
                try:
                    chunk = json.loads(line)
                    token = chunk.get("message", {}).get("content", "")
                    done  = chunk.get("done", False)
                    if token:
                        yield f"data: {json.dumps({'token': token, 'done': False})}\n\n"
                    if done:
                        yield f"data: {json.dumps({'token': '', 'done': True})}\n\n"
                        return
                except Exception:
                    continue

async def non_stream_ollama(model: str, messages: list) -> str:
    """Fallback non-streaming call."""
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(connect=TIMEOUT_CONNECT, read=TIMEOUT_READ, write=10, pool=10)
    ) as client:
        res = await client.post(f"{OLLAMA_URL}/api/chat", json={
            "model":    model,
            "messages": messages,
            "stream":   False,
            "options":  {"temperature": 0.7, "num_predict": 600}
        })
        if res.status_code == 200:
            return res.json().get("message", {}).get("content", "No response.")
        raise Exception(f"Ollama {res.status_code}")

def fallback_response(message: str) -> str:
    msg = message.lower()
    if any(w in msg for w in ["stock", "inventory", "reorder", "product"]):
        base = "Check the Inventory tab → Low Stock filter to see items needing reorder."
    elif any(w in msg for w in ["sale", "revenue", "profit", "money", "leo", "mauzo"]):
        base = "Visit the Analytics tab for revenue charts, top products, and cashier performance."
    elif any(w in msg for w in ["customer", "loyalty", "credit", "deni", "mteja"]):
        base = "Visit the Customers tab to manage profiles, loyalty points, and credit balances."
    elif any(w in msg for w in ["mpesa", "payment", "mobile", "malipo"]):
        base = "M-Pesa STK Push is built in — select M-Pesa at checkout and enter the customer's phone."
    else:
        base = "I can help with sales, inventory, pricing, and business insights."
    return (f"💡 {base}\n\n"
            f"⚠ AI model not available. To enable:\n"
            f"1. Make sure Ollama is running: `ollama serve`\n"
            f"2. Pull a model: `ollama pull llama3.2` (or llama3.1, mistral, gemma2, phi3)\n"
            f"3. Refresh the page — the AI dot will turn green when ready.")

# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/models")
async def list_models(request: Request):
    """Return all models available in Ollama."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(401, "Authentication required")
    models = await get_available_models()
    current = DEFAULT_MODEL or (models[0] if models else "")
    return {"models": models, "current": current, "ollama_running": len(models) > 0}

@router.get("/status")
async def ai_status():
    if not HTTPX_AVAILABLE:
        return {"ollama_running": False, "llama_available": False,
                "status": "httpx_not_installed",
                "fix": "Run: pip install httpx"}
    models = await get_available_models()
    if models:
        return {"ollama_running": True, "llama_available": True,
                "models": models, "status": "ready",
                "active_model": DEFAULT_MODEL or models[0]}
    # Check if Ollama is running but has no models
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_CONNECT) as client:
            res = await client.get(f"{OLLAMA_URL}/api/tags")
            if res.status_code == 200:
                return {"ollama_running": True, "llama_available": False,
                        "models": [], "status": "no_models",
                        "fix": "Run: ollama pull llama3.2"}
    except Exception:
        pass
    return {"ollama_running": False, "llama_available": False,
            "models": [], "status": "offline",
            "fix": "Run: ollama serve   (then pull a model if you haven't)"}

@router.post("/chat")
async def ai_chat(msg: AIMessage, request: Request):
    """Non-streaming chat — returns full response at once."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(401, "Authentication required")

    if not HTTPX_AVAILABLE:
        return {"response": fallback_response(msg.message), "model": "fallback"}

    model = await pick_model(msg.model or "")
    conn  = get_db()

    # Build message list: system + history + new user message
    system_prompt = get_system_context(conn, msg.context or "general")
    messages = [{"role": "system", "content": system_prompt}]

    # Append recent history (last 10 turns)
    for h in (msg.history or [])[-10:]:
        if h.get("role") in ("user", "assistant") and h.get("content"):
            messages.append({"role": h["role"], "content": h["content"]})

    messages.append({"role": "user", "content": msg.message})

    try:
        response_text = await non_stream_ollama(model, messages)
    except Exception:
        response_text = fallback_response(msg.message)
        model = "fallback"

    # Save to history
    uid = user.get("user_id") or user.get("id")
    sid = msg.session_id or ""
    try:
        conn.execute(
            "INSERT INTO ai_conversations (user_id, session_id, role, message, context) VALUES (?,?,?,?,?)",
            (uid, sid, "user", msg.message, msg.context)
        )
        conn.execute(
            "INSERT INTO ai_conversations (user_id, session_id, role, message, context) VALUES (?,?,?,?,?)",
            (uid, sid, "assistant", response_text, msg.context)
        )
        conn.commit()
    except Exception:
        pass
    conn.close()

    return {"response": response_text, "model": model,
            "timestamp": datetime.now().isoformat()}

@router.post("/chat/stream")
async def ai_chat_stream(msg: AIMessage, request: Request):
    """Streaming chat — tokens arrive word-by-word via SSE."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(401, "Authentication required")

    if not HTTPX_AVAILABLE:
        fallback = fallback_response(msg.message)
        async def fallback_stream():
            yield f"data: {json.dumps({'token': fallback, 'done': False})}\n\n"
            yield f"data: {json.dumps({'token': '', 'done': True})}\n\n"
        return StreamingResponse(fallback_stream(), media_type="text/event-stream")

    model  = await pick_model(msg.model or "")
    conn   = get_db()
    system = get_system_context(conn, msg.context or "general")
    conn.close()

    messages = [{"role": "system", "content": system}]
    for h in (msg.history or [])[-10:]:
        if h.get("role") in ("user", "assistant") and h.get("content"):
            messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": msg.message})

    # Collect full response for history storage
    full_response = []

    async def generate():
        try:
            async for chunk in stream_ollama(model, messages):
                yield chunk
                # Extract token to build full response
                try:
                    data = json.loads(chunk.replace("data: ", ""))
                    if data.get("token"):
                        full_response.append(data["token"])
                except Exception:
                    pass
        except Exception as e:
            err = fallback_response(msg.message)
            yield f"data: {json.dumps({'token': err, 'done': False})}\n\n"
            yield f"data: {json.dumps({'token': '', 'done': True, 'error': True})}\n\n"

        # Save conversation after stream completes
        try:
            db = get_db()
            uid = user.get("user_id") or user.get("id")
            sid = msg.session_id or ""
            db.execute(
                "INSERT INTO ai_conversations (user_id, session_id, role, message, context) VALUES (?,?,?,?,?)",
                (uid, sid, "user", msg.message, msg.context)
            )
            db.execute(
                "INSERT INTO ai_conversations (user_id, session_id, role, message, context) VALUES (?,?,?,?,?)",
                (uid, sid, "assistant", "".join(full_response), msg.context)
            )
            db.commit(); db.close()
        except Exception:
            pass

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )

@router.get("/history")
async def chat_history(request: Request, limit: int = 20, session_id: str = ""):
    user = get_current_user(request)
    if not user:
        raise HTTPException(401, "Authentication required")
    db  = get_db()
    uid = user.get("user_id") or user.get("id")
    q   = "SELECT role, message, created_at FROM ai_conversations WHERE user_id=?"
    p   = [uid]
    if session_id:
        q += " AND session_id=?"
        p.append(session_id)
    q += " ORDER BY created_at DESC LIMIT ?"
    p.append(limit)
    rows = db.execute(q, p).fetchall()
    db.close()
    return list(reversed([{"role": r[0], "content": r[1], "created_at": r[2]} for r in rows]))

@router.delete("/history")
async def clear_history(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(401, "Authentication required")
    db  = get_db()
    uid = user.get("user_id") or user.get("id")
    db.execute("DELETE FROM ai_conversations WHERE user_id=?", (uid,))
    db.commit(); db.close()
    return {"cleared": True}
