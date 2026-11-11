"""
Neural POS — Pricing Intelligence Router
Covers:
  1. Multi-currency: live FX rates, currency conversion, per-sale currency display
  2. Price Intelligence: scrape supplier/market prices from the web for restocking decisions
  3. Price comparison: compare your cost vs scraped market price vs suppliers

Dependencies (add to requirements.txt):
  httpx          (already present)
  beautifulsoup4
  lxml
"""

import re
import json
import logging
import asyncio
from datetime import datetime, timedelta
from typing import Optional, List
from fastapi import APIRouter, HTTPException, Request, BackgroundTasks
from pydantic import BaseModel
import httpx

from models.database import get_db
from routers.auth import get_current_user

logger = logging.getLogger("neural_pos.pricing")
router = APIRouter()

# ── Supported currencies ──────────────────────────────────────────────────────
SUPPORTED_CURRENCIES = {
    "KES": "Kenyan Shilling",
    "USD": "US Dollar",
    "EUR": "Euro",
    "GBP": "British Pound",
    "UGX": "Ugandan Shilling",
    "TZS": "Tanzanian Shilling",
    "ZAR": "South African Rand",
    "NGN": "Nigerian Naira",
    "GHS": "Ghanaian Cedi",
    "ETB": "Ethiopian Birr",
    "INR": "Indian Rupee",
    "CNY": "Chinese Yuan",
    "AED": "UAE Dirham",
}

# ── DB setup ──────────────────────────────────────────────────────────────────

def ensure_pricing_tables():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fx_rates (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            base        TEXT NOT NULL,
            target      TEXT NOT NULL,
            rate        REAL NOT NULL,
            source      TEXT DEFAULT 'open.er-api.com',
            fetched_at  TEXT DEFAULT (datetime('now')),
            UNIQUE(base, target)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS price_intelligence (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id   INTEGER,
            product_name TEXT NOT NULL,
            query        TEXT NOT NULL,
            source_url   TEXT,
            source_name  TEXT,
            price        REAL,
            currency     TEXT DEFAULT 'KES',
            price_kes    REAL,
            scraped_at   TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (product_id) REFERENCES products(id)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_price_intel_product
        ON price_intelligence(product_id)
    """)
    conn.commit()
    conn.close()

ensure_pricing_tables()


# ── FX helpers ────────────────────────────────────────────────────────────────

def get_cached_rates(base: str = "KES") -> dict:
    """Return cached FX rates for a base currency. None if stale (>2h)."""
    conn = get_db()
    rows = conn.execute(
        "SELECT target, rate, fetched_at FROM fx_rates WHERE base = ?", (base,)
    ).fetchall()
    conn.close()
    if not rows:
        return {}
    # Check freshness — use first row's timestamp
    fetched = datetime.fromisoformat(rows[0]["fetched_at"])
    if datetime.now() - fetched > timedelta(hours=2):
        return {}  # stale
    return {r["target"]: r["rate"] for r in rows}


def save_rates(base: str, rates: dict):
    conn = get_db()
    for target, rate in rates.items():
        conn.execute("""
            INSERT INTO fx_rates (base, target, rate, fetched_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(base, target) DO UPDATE SET rate=excluded.rate, fetched_at=excluded.fetched_at
        """, (base, target, rate))
    conn.commit()
    conn.close()


async def fetch_live_rates(base: str = "KES") -> dict:
    """
    Fetch live FX rates from open.er-api.com (free, no key needed).
    Falls back to exchangerate-api.com if primary fails.
    """
    sources = [
        f"https://open.er-api.com/v6/latest/{base}",
        f"https://api.exchangerate-api.com/v4/latest/{base}",
    ]
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
        for url in sources:
            try:
                r = await client.get(url)
                data = r.json()
                rates = data.get("rates") or data.get("conversion_rates")
                if rates:
                    # Filter to our supported currencies only
                    filtered = {k: v for k, v in rates.items() if k in SUPPORTED_CURRENCIES}
                    filtered[base] = 1.0
                    save_rates(base, filtered)
                    return filtered
            except Exception as e:
                logger.warning(f"FX source {url} failed: {e}")
    return {}


# ── Price scraping helpers ────────────────────────────────────────────────────

def extract_prices_from_html(html: str, currency_hint: str = "KES") -> List[dict]:
    """
    Extract price mentions from raw HTML using regex patterns.
    Works across Jumia, Jiji, Kilimall, and generic sites.
    No CSS-selector brittle parsing — pure regex on text content.
    """
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "lxml")
        # Remove scripts, styles, nav
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        text = soup.get_text(separator=" ", strip=True)
    except Exception:
        text = html  # fallback: work on raw HTML

    results = []

    # Patterns: KES 1,200 / Ksh 1200 / KSh1,200.00 / USD 45 / $45.00 / €30
    price_patterns = [
        r'(?:KES|Ksh|KSh|kes)\s*([\d,]+(?:\.\d{2})?)',
        r'(?:USD|US\$|\$)\s*([\d,]+(?:\.\d{2})?)',
        r'(?:EUR|€)\s*([\d,]+(?:\.\d{2})?)',
        r'(?:GBP|£)\s*([\d,]+(?:\.\d{2})?)',
        r'(?:UGX|USh)\s*([\d,]+(?:\.\d{2})?)',
        r'(?:TZS|TSh)\s*([\d,]+(?:\.\d{2})?)',
    ]

    currency_map = {
        "KES": "KES", "Ksh": "KES", "KSh": "KES", "kes": "KES",
        "USD": "USD", "US$": "USD", "$": "USD",
        "EUR": "EUR", "€": "EUR",
        "GBP": "GBP", "£": "GBP",
        "UGX": "UGX", "USh": "UGX",
        "TZS": "TZS", "TSh": "TZS",
    }

    seen_prices = set()
    for pattern in price_patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            raw = match.group(1).replace(",", "")
            try:
                price = float(raw)
                if price < 1 or price > 50_000_000:
                    continue
                key = (round(price, 2),)
                if key in seen_prices:
                    continue
                seen_prices.add(key)
                # Determine currency from prefix
                prefix = match.group(0).replace(match.group(1), "").strip()
                currency = currency_map.get(prefix.rstrip(), currency_hint)
                results.append({"price": price, "currency": currency})
                if len(results) >= 5:
                    return results
            except ValueError:
                continue

    return results


async def scrape_product_prices(query: str, product_id: Optional[int] = None) -> List[dict]:
    """
    Search multiple sources for pricing data on a product.
    Sources: Jumia Kenya, Jiji Kenya, Google Shopping structured data.
    Returns list of {source, url, price, currency, title}.
    """
    results = []
    search_query = query.strip().replace(" ", "+")

    sources = [
        {
            "name": "Jumia Kenya",
            "url": f"https://www.jumia.co.ke/catalog/?q={search_query}",
            "currency": "KES",
        },
        {
            "name": "Jiji Kenya",
            "url": f"https://jiji.co.ke/search?query={search_query}",
            "currency": "KES",
        },
        {
            "name": "Kilimall",
            "url": f"https://www.kilimall.co.ke/search?keywords={search_query}",
            "currency": "KES",
        },
    ]

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }

    import ssl as _ssl
    _ssl_ctx = _ssl.create_default_context()
    _ssl_ctx.check_hostname = False
    _ssl_ctx.verify_mode = _ssl.CERT_NONE

    async with httpx.AsyncClient(
        timeout=12,
        headers=headers,
        follow_redirects=True,
        verify=False,
    ) as client:
        tasks = [_fetch_source(client, s, query) for s in sources]
        source_results = await asyncio.gather(*tasks, return_exceptions=True)

    for res in source_results:
        if isinstance(res, list):
            results.extend(res)

    # Save to DB
    if results:
        conn = get_db()
        for r in results:
            conn.execute("""
                INSERT INTO price_intelligence
                    (product_id, product_name, query, source_url, source_name, price, currency, scraped_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
            """, (product_id, query, query, r.get("url"), r.get("source"), r.get("price"), r.get("currency")))
        conn.commit()
        conn.close()

    return results


async def _fetch_source(client: httpx.AsyncClient, source: dict, query: str) -> List[dict]:
    """Fetch one source and extract prices."""
    try:
        r = await client.get(source["url"])
        if r.status_code != 200:
            return []
        prices = extract_prices_from_html(r.text, source["currency"])
        return [
            {
                "source": source["name"],
                "url": source["url"],
                "price": p["price"],
                "currency": p["currency"],
            }
            for p in prices
        ]
    except Exception as e:
        logger.warning(f"Scrape failed for {source['name']}: {e}")
        return []


# ── API Endpoints ─────────────────────────────────────────────────────────────

@router.get("/currencies")
async def list_currencies(request: Request):
    """List all supported currencies with names."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(401, "Authentication required")
    return [{"code": k, "name": v} for k, v in SUPPORTED_CURRENCIES.items()]


@router.get("/fx-rates")
async def get_fx_rates(request: Request, base: str = "KES"):
    """
    Return FX rates for a base currency.
    Uses cached rates if fresh (<2h), otherwise fetches live.
    """
    user = get_current_user(request)
    if not user:
        raise HTTPException(401, "Authentication required")

    base = base.upper()
    if base not in SUPPORTED_CURRENCIES:
        raise HTTPException(400, f"Unsupported currency: {base}")

    cached = get_cached_rates(base)
    if cached:
        return {"base": base, "rates": cached, "source": "cache"}

    live = await fetch_live_rates(base)
    if live:
        return {"base": base, "rates": live, "source": "live"}

    raise HTTPException(503, "Could not fetch exchange rates. Check internet connection.")


@router.post("/fx-rates/refresh")
async def refresh_fx_rates(request: Request, base: str = "KES"):
    """Force-refresh FX rates from live source."""
    user = get_current_user(request)
    if not user or user.get("role") not in ("admin", "manager"):
        raise HTTPException(403, "Manager/Admin required")

    base = base.upper()
    live = await fetch_live_rates(base)
    if not live:
        raise HTTPException(503, "Could not fetch exchange rates")
    return {"base": base, "rates": live, "source": "live", "refreshed_at": datetime.now().isoformat()}


@router.post("/convert")
async def convert_currency(request: Request):
    """Convert an amount between two currencies."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(401, "Authentication required")

    body = await request.json()
    amount   = float(body.get("amount", 0))
    from_cur = body.get("from", "KES").upper()
    to_cur   = body.get("to", "USD").upper()

    if from_cur == to_cur:
        return {"amount": amount, "converted": amount, "rate": 1.0, "from": from_cur, "to": to_cur}

    # Get rates with KES as base
    rates = get_cached_rates("KES") or await fetch_live_rates("KES")
    if not rates:
        raise HTTPException(503, "Exchange rates unavailable")

    # Convert: amount in from_cur → KES → to_cur
    rate_from = rates.get(from_cur, 1.0)   # from_cur per KES
    rate_to   = rates.get(to_cur, 1.0)     # to_cur per KES

    # amount in from_cur → amount_kes → amount in to_cur
    amount_kes  = amount / rate_from if from_cur != "KES" else amount
    converted   = amount_kes * rate_to if to_cur != "KES" else amount_kes
    cross_rate  = rate_to / rate_from if from_cur != "KES" else rate_to

    return {
        "amount":    amount,
        "converted": round(converted, 4),
        "rate":      round(cross_rate, 6),
        "from":      from_cur,
        "to":        to_cur,
    }


@router.post("/scrape-prices")
async def scrape_prices(request: Request, background_tasks: BackgroundTasks):
    """
    Trigger web scraping for a product's market price.
    Returns cached results immediately if available, fires fresh scrape in background.
    """
    user = get_current_user(request)
    if not user or user.get("role") not in ("admin", "manager"):
        raise HTTPException(403, "Manager/Admin required")

    body = await request.json()
    query      = body.get("query", "").strip()
    product_id = body.get("product_id")

    if not query:
        raise HTTPException(400, "query is required")

    # Return cached results first (last 24h)
    conn = get_db()
    cached = conn.execute("""
        SELECT source_name, source_url, price, currency, scraped_at
        FROM price_intelligence
        WHERE (product_id = ? OR query LIKE ?)
          AND scraped_at >= datetime('now', '-24 hours')
        ORDER BY scraped_at DESC LIMIT 20
    """, (product_id, f"%{query}%")).fetchall()
    conn.close()

    cached_results = [dict(r) for r in cached]

    # Always fire a fresh scrape in the background
    background_tasks.add_task(scrape_product_prices, query, product_id)

    return {
        "query":   query,
        "cached":  cached_results,
        "scraping": True,
        "message": "Scraping in progress — refresh in a few seconds for live results",
    }


@router.get("/scrape-results/{product_id}")
async def get_scrape_results(product_id: int, request: Request):
    """Get the latest scraped price results for a product."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(401, "Authentication required")

    conn = get_db()
    rows = conn.execute("""
        SELECT source_name, source_url, price, currency, scraped_at
        FROM price_intelligence
        WHERE product_id = ?
        ORDER BY scraped_at DESC LIMIT 30
    """, (product_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@router.get("/scrape-results/query/{query}")
async def get_scrape_results_by_query(query: str, request: Request):
    """Get scraped price results by search query."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(401, "Authentication required")

    conn = get_db()
    rows = conn.execute("""
        SELECT source_name, source_url, price, currency, scraped_at
        FROM price_intelligence
        WHERE query LIKE ?
        ORDER BY scraped_at DESC LIMIT 30
    """, (f"%{query}%",)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@router.get("/market-comparison/{product_id}")
async def market_comparison(product_id: int, request: Request):
    """
    Full price comparison for a product:
    - Your current cost price
    - Your selling price
    - Supplier's last PO unit cost
    - Scraped market prices from web
    - Recommended action
    """
    user = get_current_user(request)
    if not user:
        raise HTTPException(401, "Authentication required")

    conn = get_db()

    # Product info
    product = conn.execute("""
        SELECT p.*, s.name as supplier_name
        FROM products p LEFT JOIN suppliers s ON p.supplier_id = s.id
        WHERE p.id = ?
    """, (product_id,)).fetchone()
    if not product:
        conn.close()
        raise HTTPException(404, "Product not found")
    product = dict(product)

    # Last PO cost for this product
    last_po = conn.execute("""
        SELECT poi.unit_cost, po.created_at, s.name as supplier_name
        FROM purchase_order_items poi
        JOIN purchase_orders po ON poi.purchase_order_id = po.id
        JOIN suppliers s ON po.supplier_id = s.id
        WHERE poi.product_id = ?
        ORDER BY po.created_at DESC LIMIT 1
    """, (product_id,)).fetchone()

    # Scraped market prices (last 7 days)
    market_prices = conn.execute("""
        SELECT source_name, price, currency, scraped_at
        FROM price_intelligence
        WHERE product_id = ? AND scraped_at >= datetime('now', '-7 days')
        ORDER BY scraped_at DESC LIMIT 10
    """, (product_id,)).fetchall()
    conn.close()

    market_list = [dict(r) for r in market_prices]
    avg_market  = (sum(r["price"] for r in market_list) / len(market_list)) if market_list else None

    # Recommendation logic
    recommendation = None
    if avg_market and product["cost"]:
        diff_pct = ((product["cost"] - avg_market) / avg_market) * 100
        if diff_pct > 20:
            recommendation = f"⚠️ Your cost is {diff_pct:.1f}% above market average — consider switching suppliers or renegotiating."
        elif diff_pct < -20:
            recommendation = f"✅ Your cost is {abs(diff_pct):.1f}% below market average — good deal from your supplier."
        else:
            recommendation = f"Your cost is within {abs(diff_pct):.1f}% of market average — competitive."

    return {
        "product":          product,
        "your_cost":        product["cost"],
        "your_price":       product["price"],
        "last_po_cost":     dict(last_po) if last_po else None,
        "market_prices":    market_list,
        "avg_market_price": round(avg_market, 2) if avg_market else None,
        "recommendation":   recommendation,
    }
