"""
Neural POS v9 — Production-hardened entry point
Security: env-based secrets, CORS lockdown, security headers,
          rate limiting, access logging middleware
Reliability: DB WAL mode, health endpoint, auto-daily backup
"""
import os, time, datetime, logging
from contextlib import asynccontextmanager

# Load .env before anything else
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
import uvicorn
import collections

# ── Auto-create directories ───────────────────────────────────────────────────
for _d in ("data", "data/backups", "static", "static/css", "static/js", "static/img", "logs"):
    os.makedirs(_d, exist_ok=True)

# ── Logging to file ───────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.FileHandler("logs/neural_pos.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("neural_pos")

# ── Import routers ────────────────────────────────────────────────────────────
from routers import (auth, pos, inventory, suppliers, analytics, admin,
                     ai_assistant, invoices, customers, expenses, staff,
                     returns, mpesa, notifications, reports, variants, sms,
                     eod, credit_sales, alerts, pricelist,
                     settings, promotions, inventory_extras, access_log,
                     payroll, hardware, pricing, media, barcodes,
                     bundles, csv_import)
from models.database import init_db, backup_db

# ── CORS config (from .env) ───────────────────────────────────────────────────
_origins_raw = os.getenv("CORS_ORIGINS", "*")
CORS_ORIGINS  = [o.strip() for o in _origins_raw.split(",")] if _origins_raw != "*" else ["*"]

# ── Global API rate limiting middleware ──────────────────────────────────────
class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Simple sliding-window rate limiter per IP.
    Defaults: 300 requests/60s for all API routes.
    Stricter: 20 requests/60s for auth routes (login handled separately).
    """
    _windows: dict = collections.defaultdict(list)
    LIMIT_API    = 300
    LIMIT_AUTH   = 20
    WINDOW       = 60  # seconds

    async def dispatch(self, request: Request, call_next):
        if not request.url.path.startswith("/api"):
            return await call_next(request)

        ip  = (request.headers.get("X-Forwarded-For") or
               getattr(request.client, "host", "0.0.0.0"))
        key = f"{ip}:{request.url.path}"
        now = time.time()
        limit = self.LIMIT_AUTH if request.url.path.startswith("/api/auth") else self.LIMIT_API

        hits = [t for t in self._windows[key] if now - t < self.WINDOW]
        self._windows[key] = hits
        if len(hits) >= limit:
            return JSONResponse(
                {"detail": "Rate limit exceeded. Please slow down."},
                status_code=429
            )
        self._windows[key].append(now)
        return await call_next(request)


# ── Security headers middleware ───────────────────────────────────────────────
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)
        response.headers["X-Content-Type-Options"]  = "nosniff"
        response.headers["X-Frame-Options"]         = "SAMEORIGIN"
        response.headers["X-XSS-Protection"]        = "1; mode=block"
        response.headers["Referrer-Policy"]         = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"]      = "camera=(), microphone=(), geolocation=()"
        # Don't set HSTS in dev (breaks HTTP LAN access); enable in prod behind HTTPS
        # response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

# ── Request timing + access log middleware ────────────────────────────────────
class RequestLogMiddleware(BaseHTTPMiddleware):
    SENSITIVE_PATHS = {"/api/auth/login", "/api/auth/logout"}

    async def dispatch(self, request: Request, call_next):
        start = time.time()
        response = await call_next(request)
        duration = round((time.time() - start) * 1000, 1)

        # Log every API call (skip static)
        if request.url.path.startswith("/api"):
            logger.info(
                f"{request.method} {request.url.path} "
                f"→ {response.status_code}  [{duration}ms]  "
                f"IP:{request.headers.get('X-Forwarded-For', getattr(request.client,'host','?'))}"
            )

        return response

# ── Daily backup task (simple in-process scheduler) ──────────────────────────
_last_backup_date: str = ""

async def maybe_backup():
    global _last_backup_date
    today = datetime.date.today().isoformat()
    if _last_backup_date != today:
        try:
            path = backup_db()
            _last_backup_date = today
            logger.info(f"Auto-backup written → {path}")
        except Exception as e:
            logger.error(f"Backup failed: {e}")

# ── App factory ───────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Neural POS starting up…")
    init_db()
    await maybe_backup()
    logger.info("Database initialised ✓")
    yield
    logger.info("Neural POS shutting down.")

app = FastAPI(
    title="Neural POS",
    description="Intelligent Point of Sale — Kenya",
    version="9.0.0",
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url=None,
)

# Middleware order matters — add innermost last (outermost runs first)
app.add_middleware(RateLimitMiddleware)
app.add_middleware(RequestLogMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Static + templates ────────────────────────────────────────────────────────
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# ── Service Worker — served from root with correct scope header ───────────────
@app.get("/sw.js", include_in_schema=False)
async def service_worker():
    from fastapi.responses import FileResponse
    return FileResponse(
        "static/sw.js",
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/"}
    )

# ── Routers ───────────────────────────────────────────────────────────────────
app.include_router(auth.router,             prefix="/api/auth",        tags=["Auth"])
app.include_router(pos.router,              prefix="/api/pos",         tags=["POS"])
app.include_router(inventory.router,        prefix="/api/inventory",   tags=["Inventory"])
app.include_router(suppliers.router,        prefix="/api/suppliers",   tags=["Suppliers"])
app.include_router(analytics.router,        prefix="/api/analytics",   tags=["Analytics"])
app.include_router(admin.router,            prefix="/api/admin",       tags=["Admin"])
app.include_router(ai_assistant.router,     prefix="/api/ai",          tags=["AI"])
app.include_router(invoices.router,         prefix="/api/invoices",    tags=["Invoices"])
app.include_router(customers.router,        prefix="/api/customers",   tags=["Customers"])
app.include_router(expenses.router,         prefix="/api/expenses",    tags=["Expenses"])
app.include_router(staff.router,            prefix="/api/staff",       tags=["Staff"])
app.include_router(returns.router,          prefix="/api/returns",     tags=["Returns"])
app.include_router(mpesa.router,            prefix="/api/mpesa",       tags=["M-Pesa"])
app.include_router(notifications.router,    prefix="/api/notifications",tags=["Notifications"])
app.include_router(reports.router,          prefix="/api/reports",     tags=["Reports"])
app.include_router(variants.router,         prefix="/api/variants",    tags=["Variants"])
app.include_router(sms.router,              prefix="/api/sms",         tags=["SMS"])
app.include_router(eod.router,              prefix="/api/eod",         tags=["EOD"])
app.include_router(credit_sales.router,     prefix="/api/credit",      tags=["Credit"])
app.include_router(alerts.router,           prefix="/api/alerts",      tags=["Alerts"])
app.include_router(pricelist.router,        prefix="/api/pricelist",   tags=["Price List"])
app.include_router(settings.router,         prefix="/api/settings",    tags=["Settings"])
app.include_router(promotions.router,       prefix="/api/promotions",  tags=["Promotions"])
app.include_router(inventory_extras.router, prefix="/api/inv",         tags=["Inv Extras"])
app.include_router(access_log.router,       prefix="/api/access-log",  tags=["Access Log"])
app.include_router(payroll.router,          prefix="/api/payroll",     tags=["Payroll"])
app.include_router(hardware.router,         prefix="/api/hardware",    tags=["Hardware"])
app.include_router(pricing.router,          prefix="/api/pricing",     tags=["Pricing"])
app.include_router(media.router,            prefix="/api/media",       tags=["Media"])
app.include_router(barcodes.router,         prefix="/api/barcodes",    tags=["Barcodes"])
app.include_router(bundles.router,          prefix="/api/bundles",     tags=["Bundles"])
app.include_router(csv_import.router,       prefix="/api/import",      tags=["Import"])

# ── Pages ─────────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    await maybe_backup()   # opportunistic daily backup
    return templates.TemplateResponse("dashboard.html", {"request": request})

# ── Health & diagnostics ──────────────────────────────────────────────────────
@app.get("/health")
async def health():
    from models.database import DB_PATH
    db_size = os.path.getsize(DB_PATH) // 1024 if os.path.exists(DB_PATH) else 0
    return {
        "status": "ok",
        "version": "9.0.0",
        "db_size_kb": db_size,
        "timestamp": datetime.datetime.now().isoformat(),
    }

@app.get("/api/ping")
async def ping():
    return {"ok": True, "message": "Neural POS is running"}

if __name__ == "__main__":
    host  = os.getenv("HOST", "0.0.0.0")
    port  = int(os.getenv("PORT", "8000"))
    debug = os.getenv("DEBUG", "true").lower() == "true"
    uvicorn.run("main:app", host=host, port=port, reload=debug)
