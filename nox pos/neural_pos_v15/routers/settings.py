"""
Neural POS — Settings Module
Business config, receipt customisation, tax rates, language, backup
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
from models.database import get_db
from routers.auth import get_current_user

router = APIRouter()

class SettingsUpdate(BaseModel):
    settings: dict   # key-value pairs to save

def get_all_settings(db):
    rows = db.execute("SELECT key, value FROM settings").fetchall()
    return {r[0]: r[1] for r in rows}

@router.get("")
async def read_settings(current_user=Depends(get_current_user)):
    db = get_db()
    s = get_all_settings(db)
    db.close()
    return s

@router.put("")
async def update_settings(data: SettingsUpdate, current_user=Depends(get_current_user)):
    if current_user.get("role") not in ["admin", "manager"]:
        raise HTTPException(403, "Admin or Manager required")
    db = get_db()
    for key, value in data.settings.items():
        db.execute("INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, datetime('now'))",
                   (key, str(value)))
    db.commit()
    s = get_all_settings(db)
    db.close()
    return s

@router.get("/backup")
async def backup_data(current_user=Depends(get_current_user)):
    """Export full database as JSON for cloud backup."""
    if current_user.get("role") != "admin":
        raise HTTPException(403, "Admin required")
    import json, datetime
    db = get_db()
    backup = {"exported_at": datetime.datetime.now().isoformat(), "tables": {}}
    tables = ["users","products","suppliers","orders","order_items",
              "customers","expenses","invoices","credit_sales","cash_ups","settings"]
    for t in tables:
        try:
            rows = db.execute(f"SELECT * FROM {t}").fetchall()
            backup["tables"][t] = [dict(r) for r in rows]
        except: pass
    db.close()
    return backup
