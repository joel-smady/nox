from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional, List
from models.database import get_db
from routers.auth import get_current_user
import datetime

router = APIRouter()

class InvoiceItem(BaseModel):
    description: str
    quantity: float
    unit_price: float

class InvoiceCreate(BaseModel):
    client_name: str
    client_email: Optional[str] = ""
    client_address: Optional[str] = ""
    issue_date: str
    due_date: str
    tax_rate: Optional[float] = 16.0
    status: Optional[str] = "draft"
    notes: Optional[str] = ""
    items: List[InvoiceItem]

def ensure_invoice_tables():
    db = get_db()
    db.execute("""
        CREATE TABLE IF NOT EXISTS invoices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_number TEXT UNIQUE NOT NULL,
            client_name TEXT NOT NULL,
            client_email TEXT DEFAULT '',
            client_address TEXT DEFAULT '',
            issue_date TEXT NOT NULL,
            due_date TEXT NOT NULL,
            subtotal REAL DEFAULT 0,
            tax_rate REAL DEFAULT 16,
            tax_amount REAL DEFAULT 0,
            total_amount REAL DEFAULT 0,
            status TEXT DEFAULT 'draft',
            notes TEXT DEFAULT '',
            created_by INTEGER,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS invoice_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_id INTEGER NOT NULL,
            description TEXT NOT NULL,
            quantity REAL NOT NULL,
            unit_price REAL NOT NULL,
            total REAL NOT NULL,
            FOREIGN KEY (invoice_id) REFERENCES invoices(id) ON DELETE CASCADE
        )
    """)
    db.commit()
    db.close()

ensure_invoice_tables()

def next_invoice_number(db):
    row = db.execute("SELECT COUNT(*) FROM invoices").fetchone()
    return f"INV-{row[0]+1:05d}"

def compute_totals(items, tax_rate):
    subtotal = sum(i.quantity * i.unit_price for i in items)
    tax_amount = round(subtotal * tax_rate / 100, 2)
    return round(subtotal, 2), tax_amount, round(subtotal + tax_amount, 2)

def row_to_dict(row, items):
    return {
        "id": row[0], "invoice_number": row[1], "client_name": row[2],
        "client_email": row[3], "client_address": row[4], "issue_date": row[5],
        "due_date": row[6], "subtotal": row[7], "tax_rate": row[8],
        "tax_amount": row[9], "total_amount": row[10], "status": row[11],
        "notes": row[12], "created_at": row[13], "items": items
    }

def fetch_items(db, invoice_id):
    rows = db.execute(
        "SELECT description,quantity,unit_price,total FROM invoice_items WHERE invoice_id=? ORDER BY id",
        (invoice_id,)
    ).fetchall()
    return [{"description": r[0], "quantity": r[1], "unit_price": r[2], "total": r[3]} for r in rows]

SELECT_COLS = "id,invoice_number,client_name,client_email,client_address,issue_date,due_date,subtotal,tax_rate,tax_amount,total_amount,status,notes,created_at"

def auto_overdue(db):
    today = datetime.date.today().isoformat()
    db.execute("UPDATE invoices SET status='overdue',updated_at=datetime('now') WHERE due_date<? AND status IN ('sent','draft')", (today,))
    db.commit()

@router.get("")
async def list_invoices(status: Optional[str] = None, current_user=Depends(get_current_user)):
    db = get_db()
    auto_overdue(db)
    q = f"SELECT {SELECT_COLS} FROM invoices"
    params = []
    if status:
        q += " WHERE status=?"; params.append(status)
    q += " ORDER BY id DESC"
    rows = db.execute(q, params).fetchall()
    result = [row_to_dict(r, fetch_items(db, r[0])) for r in rows]
    db.close()
    return result

@router.get("/stats")
async def invoice_stats(current_user=Depends(get_current_user)):
    db = get_db()
    auto_overdue(db)
    total    = db.execute("SELECT COUNT(*) FROM invoices").fetchone()[0]
    paid     = db.execute("SELECT COALESCE(SUM(total_amount),0) FROM invoices WHERE status='paid'").fetchone()[0]
    pending  = db.execute("SELECT COALESCE(SUM(total_amount),0) FROM invoices WHERE status IN ('sent','draft')").fetchone()[0]
    overdue  = db.execute("SELECT COUNT(*) FROM invoices WHERE status='overdue'").fetchone()[0]
    db.close()
    return {"total": total, "paid_amount": paid, "pending_amount": pending, "overdue": overdue}

@router.get("/{invoice_id}")
async def get_invoice(invoice_id: int, current_user=Depends(get_current_user)):
    db = get_db()
    row = db.execute(f"SELECT {SELECT_COLS} FROM invoices WHERE id=?", (invoice_id,)).fetchone()
    if not row:
        db.close(); raise HTTPException(404, "Invoice not found")
    result = row_to_dict(row, fetch_items(db, invoice_id))
    db.close()
    return result

@router.post("")
async def create_invoice(data: InvoiceCreate, current_user=Depends(get_current_user)):
    db = get_db()
    subtotal, tax_amount, total = compute_totals(data.items, data.tax_rate or 0)
    inv_num = next_invoice_number(db)
    db.execute(
        "INSERT INTO invoices (invoice_number,client_name,client_email,client_address,issue_date,due_date,subtotal,tax_rate,tax_amount,total_amount,status,notes,created_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (inv_num, data.client_name, data.client_email, data.client_address,
         data.issue_date, data.due_date, subtotal, data.tax_rate or 0,
         tax_amount, total, data.status or "draft", data.notes or "", current_user["id"])
    )
    invoice_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    for item in data.items:
        db.execute(
            "INSERT INTO invoice_items (invoice_id,description,quantity,unit_price,total) VALUES (?,?,?,?,?)",
            (invoice_id, item.description, item.quantity, item.unit_price, round(item.quantity * item.unit_price, 2))
        )
    db.commit()
    row = db.execute(f"SELECT {SELECT_COLS} FROM invoices WHERE id=?", (invoice_id,)).fetchone()
    result = row_to_dict(row, fetch_items(db, invoice_id))
    db.close()
    return result

@router.put("/{invoice_id}")
async def update_invoice(invoice_id: int, data: InvoiceCreate, current_user=Depends(get_current_user)):
    db = get_db()
    if not db.execute("SELECT id FROM invoices WHERE id=?", (invoice_id,)).fetchone():
        db.close(); raise HTTPException(404, "Invoice not found")
    subtotal, tax_amount, total = compute_totals(data.items, data.tax_rate or 0)
    db.execute(
        "UPDATE invoices SET client_name=?,client_email=?,client_address=?,issue_date=?,due_date=?,subtotal=?,tax_rate=?,tax_amount=?,total_amount=?,status=?,notes=?,updated_at=datetime('now') WHERE id=?",
        (data.client_name, data.client_email, data.client_address,
         data.issue_date, data.due_date, subtotal, data.tax_rate or 0,
         tax_amount, total, data.status or "draft", data.notes or "", invoice_id)
    )
    db.execute("DELETE FROM invoice_items WHERE invoice_id=?", (invoice_id,))
    for item in data.items:
        db.execute(
            "INSERT INTO invoice_items (invoice_id,description,quantity,unit_price,total) VALUES (?,?,?,?,?)",
            (invoice_id, item.description, item.quantity, item.unit_price, round(item.quantity * item.unit_price, 2))
        )
    db.commit()
    row = db.execute(f"SELECT {SELECT_COLS} FROM invoices WHERE id=?", (invoice_id,)).fetchone()
    result = row_to_dict(row, fetch_items(db, invoice_id))
    db.close()
    return result

@router.delete("/{invoice_id}")
async def delete_invoice(invoice_id: int, current_user=Depends(get_current_user)):
    db = get_db()
    if not db.execute("SELECT id FROM invoices WHERE id=?", (invoice_id,)).fetchone():
        db.close(); raise HTTPException(404, "Invoice not found")
    db.execute("DELETE FROM invoice_items WHERE invoice_id=?", (invoice_id,))
    db.execute("DELETE FROM invoices WHERE id=?", (invoice_id,))
    db.commit()
    db.close()
    return {"deleted": True}
