"""
Neural POS — Price List & Quotation Generator
Printable price lists, customer quotations, PDF-ready HTML
"""
from fastapi import APIRouter, Depends
from fastapi.responses import Response
from typing import Optional
from models.database import get_db
from routers.auth import get_current_user
import datetime

router = APIRouter()

@router.get("/products")
async def get_price_list(category: Optional[str] = None,
                          current_user=Depends(get_current_user)):
    """JSON price list — for dashboard table."""
    db = get_db()
    q = "SELECT id,sku,name,category,price,stock_quantity FROM products WHERE is_active=1"
    params = []
    if category: q += " AND category=?"; params.append(category)
    q += " ORDER BY category, name"
    rows = db.execute(q, params).fetchall()
    db.close()
    return [dict(zip(["id","sku","name","category","price","stock_quantity"], r)) for r in rows]

@router.get("/print", response_class=Response)
async def print_price_list(category: Optional[str] = None,
                            title: Optional[str] = "Price List",
                            show_stock: int = 0,
                            current_user=Depends(get_current_user)):
    """Returns a printable HTML page — open in browser and Ctrl+P."""
    db = get_db()
    q = "SELECT sku,name,category,price,stock_quantity FROM products WHERE is_active=1"
    params = []
    if category: q += " AND category=?"; params.append(category)
    q += " ORDER BY category, name"
    rows = db.execute(q, params).fetchall()

    # Group by category
    cats = {}
    for r in rows:
        c = r[2] or "General"
        if c not in cats: cats[c] = []
        cats[c].append(r)

    today = datetime.date.today().strftime("%d %B %Y")
    business = db.execute("SELECT value FROM settings WHERE key='business_name'").fetchone()
    biz_name = business[0] if business else "Neural POS"
    db.close()

    table_rows = ""
    row_num = 1
    for cat, products in cats.items():
        table_rows += f'<tr class="cat-header"><td colspan="{4 if show_stock else 3}">{cat}</td></tr>\n'
        for p in products:
            stock_td = f'<td class="center">{p[4]}</td>' if show_stock else ""
            table_rows += f"""
            <tr>
                <td class="num">{row_num}</td>
                <td>{p[1]}<br><small>{p[0]}</small></td>
                <td class="right">KES {p[3]:,.2f}</td>
                {stock_td}
            </tr>"""
            row_num += 1

    stock_header = '<th class="center">Stock</th>' if show_stock else ""
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>{title} — {biz_name}</title>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ font-family: Arial, sans-serif; font-size: 11pt; color: #111; padding: 20mm; }}
  h1 {{ font-size: 20pt; color: #2d1b69; margin-bottom: 4px; }}
  .subtitle {{ color: #666; font-size: 9pt; margin-bottom: 16px; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 12px; }}
  th {{ background: #2d1b69; color: white; padding: 8px 10px; text-align: left; font-size: 10pt; }}
  td {{ padding: 6px 10px; border-bottom: 1px solid #eee; font-size: 10pt; }}
  tr:nth-child(even) td {{ background: #f9f9f9; }}
  .cat-header td {{ background: #e8e4f8; font-weight: bold; color: #2d1b69; padding: 6px 10px; font-size: 9pt; text-transform: uppercase; letter-spacing: 1px; }}
  .num {{ color: #999; width: 32px; text-align: right; }}
  .right {{ text-align: right; font-weight: 600; color: #2d1b69; width: 130px; }}
  .center {{ text-align: center; width: 70px; }}
  small {{ color: #888; font-size: 8pt; }}
  .footer {{ margin-top: 24px; font-size: 8pt; color: #999; text-align: center; border-top: 1px solid #eee; padding-top: 10px; }}
  @media print {{ body {{ padding: 10mm; }} .no-print {{ display: none; }} }}
</style>
</head>
<body>
  <div class="no-print" style="background:#2d1b69;color:white;padding:10px 20px;margin:-20mm -20mm 16px;display:flex;justify-content:space-between;align-items:center">
    <span style="font-weight:bold">Neural POS — Price List</span>
    <button onclick="window.print()" style="background:white;color:#2d1b69;border:none;padding:6px 16px;border-radius:4px;cursor:pointer;font-weight:bold">🖨 Print</button>
  </div>
  <h1>{biz_name}</h1>
  <div class="subtitle">{title} &nbsp;·&nbsp; Generated: {today}</div>
  <table>
    <thead>
      <tr>
        <th class="num">#</th>
        <th>Product</th>
        <th class="right">Price</th>
        {stock_header}
      </tr>
    </thead>
    <tbody>
      {table_rows}
    </tbody>
  </table>
  <div class="footer">Prices inclusive of 16% VAT &nbsp;·&nbsp; {biz_name} &nbsp;·&nbsp; {today}</div>
</body>
</html>"""
    return Response(content=html, media_type="text/html")

@router.get("/quotation/{customer_id}", response_class=Response)
async def print_quotation(customer_id: int,
                           item_ids: Optional[str] = None,
                           current_user=Depends(get_current_user)):
    """Generate a printable quotation for a specific customer."""
    db = get_db()
    cust = db.execute("SELECT name, phone, email, address FROM customers WHERE id=?",
                       (customer_id,)).fetchone()
    if not cust:
        return Response(content="<h1>Customer not found</h1>", media_type="text/html")

    # Get products — all or selected
    if item_ids:
        ids = [int(x) for x in item_ids.split(",") if x.strip().isdigit()]
        placeholders = ",".join("?" * len(ids))
        rows = db.execute(f"SELECT sku,name,category,price FROM products WHERE id IN ({placeholders}) AND is_active=1", ids).fetchall()
    else:
        rows = db.execute("SELECT sku,name,category,price FROM products WHERE is_active=1 ORDER BY category,name LIMIT 20").fetchall()

    biz = db.execute("SELECT value FROM settings WHERE key='business_name'").fetchone()
    biz_name = biz[0] if biz else "Neural POS"
    phone = db.execute("SELECT value FROM settings WHERE key='business_phone'").fetchone()
    biz_phone = phone[0] if phone else ""
    db.close()

    today = datetime.date.today().strftime("%d %B %Y")
    valid_until = (datetime.date.today() + datetime.timedelta(days=14)).strftime("%d %B %Y")
    quote_num = f"QT-{datetime.date.today().strftime('%Y%m%d')}-{customer_id:03d}"

    rows_html = "".join(f"""
    <tr>
      <td>{i+1}</td>
      <td>{r[1]}<br><small style="color:#888">{r[0]}</small></td>
      <td style="text-align:center"></td>
      <td style="text-align:right">KES {r[3]:,.2f}</td>
      <td style="text-align:right"></td>
    </tr>""" for i, r in enumerate(rows))

    html = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Quotation {quote_num}</title>
<style>
  body {{ font-family: Arial, sans-serif; font-size: 11pt; padding: 20mm; color: #111; }}
  .header {{ display: flex; justify-content: space-between; margin-bottom: 24px; }}
  h1 {{ color: #2d1b69; font-size: 22pt; }}
  .label {{ font-size: 8pt; color: #888; text-transform: uppercase; letter-spacing: 1px; }}
  .value {{ font-size: 11pt; color: #111; margin-top: 2px; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 16px; }}
  th {{ background: #2d1b69; color: white; padding: 8px 10px; text-align: left; }}
  td {{ padding: 7px 10px; border-bottom: 1px solid #eee; }}
  tfoot td {{ font-weight: bold; border-top: 2px solid #2d1b69; }}
  .footer {{ margin-top: 24px; font-size: 8pt; color: #888; border-top: 1px solid #eee; padding-top: 10px; }}
  @media print {{ .no-print {{ display:none; }} }}
</style></head>
<body>
<div class="no-print" style="background:#2d1b69;color:white;padding:10px 20px;margin:-20mm -20mm 16px;display:flex;justify-content:space-between">
  <span>Quotation — Neural POS</span>
  <button onclick="window.print()" style="background:white;color:#2d1b69;border:none;padding:6px 16px;border-radius:4px;cursor:pointer;font-weight:bold">🖨 Print</button>
</div>
<div class="header">
  <div><h1>{biz_name}</h1><div style="color:#888;font-size:10pt">{biz_phone}</div></div>
  <div style="text-align:right">
    <div style="font-size:18pt;font-weight:bold;color:#2d1b69">QUOTATION</div>
    <div class="label">Quote #</div><div class="value">{quote_num}</div>
    <div class="label">Date</div><div class="value">{today}</div>
    <div class="label">Valid until</div><div class="value">{valid_until}</div>
  </div>
</div>
<div style="background:#f5f0ff;padding:12px 16px;border-radius:6px;margin-bottom:16px">
  <div class="label">Prepared for</div>
  <div style="font-size:13pt;font-weight:bold;margin-top:4px">{cust[0]}</div>
  <div style="color:#666">{cust[1] or ''} &nbsp; {cust[2] or ''}</div>
  {f'<div style="color:#666">{cust[3]}</div>' if cust[3] else ''}
</div>
<table>
  <thead><tr><th>#</th><th>Description</th><th style="text-align:center">Qty</th><th style="text-align:right">Unit Price</th><th style="text-align:right">Total</th></tr></thead>
  <tbody>{rows_html}</tbody>
  <tfoot><tr><td colspan="4" style="text-align:right">TOTAL (incl. 16% VAT)</td><td style="text-align:right;color:#2d1b69">KES ____.__</td></tr></tfoot>
</table>
<div class="footer">
  This quotation is valid until {valid_until}. Prices include 16% VAT.<br>
  {biz_name} · {biz_phone}
</div>
</body></html>"""
    return Response(content=html, media_type="text/html")
