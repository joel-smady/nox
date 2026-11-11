"""
Neural POS — Payroll Module
Full payroll processing for Kenyan businesses:
· Staff salary/wage configuration (monthly, hourly, or daily)
· Automatic PAYE tax calculation (Kenya KRA bands)
· NSSF deduction (KES 200 employee / KES 200 employer)
· NHIF deduction (income-based bands)
· Housing Levy (1.5% of gross)
· Custom allowances (house, transport, airtime, other)
· Custom deductions (loan repayment, advance, SACCO, etc.)
· Payslip generation (printable HTML)
· Payroll run → auto-posts to expenses
· Pay history per employee
"""
from fastapi import APIRouter, HTTPException, Depends, Request
from fastapi.responses import Response
from pydantic import BaseModel
from typing import Optional, List
from models.database import get_db
from routers.deps import get_current_user
import datetime, json

router = APIRouter()

# ─────────────────────────────────────────────────────────────────────────────
# Kenya tax tables (FY 2024/25)
# ─────────────────────────────────────────────────────────────────────────────

# PAYE bands (monthly taxable income → rate)
PAYE_BANDS = [
    (24000,  0.10),
    (8333,   0.25),
    (467667, 0.30),
    (float('inf'), 0.325),
]
PERSONAL_RELIEF = 2400   # KES/month

def calc_paye(taxable_income: float) -> float:
    """Kenya PAYE on monthly taxable income."""
    tax = 0.0
    remaining = taxable_income
    for band_max, rate in PAYE_BANDS:
        if remaining <= 0:
            break
        taxable = min(remaining, band_max)
        tax += taxable * rate
        remaining -= taxable
    tax -= PERSONAL_RELIEF
    return max(0.0, round(tax, 2))

# NHIF bands (monthly gross → deduction)
NHIF_BANDS = [
    (5999,   150),   (7999,  300),   (11999,  400),
    (14999,  500),   (19999,  600),  (24999,  750),
    (29999,  850),   (34999,  900),  (39999,  950),
    (44999, 1000),   (49999, 1100),  (59999, 1200),
    (69999, 1300),   (79999, 1400),  (89999, 1500),
    (99999, 1600),   (float('inf'), 1700),
]

def calc_nhif(gross: float) -> float:
    for ceiling, deduction in NHIF_BANDS:
        if gross <= ceiling:
            return float(deduction)
    return 1700.0

# ── NSSF Act 2013 (tiered, enforced from Feb 2023) ──────────────────────────
# Lower Earnings Limit (LEL) = KES 6,000 | Upper Earnings Limit (UEL) = KES 18,000
# Tier I : 6% of earnings up to LEL (max KES 360 each — employee & employer)
# Tier II: 6% of earnings between LEL and UEL (max KES 720 each)
NSSF_LEL = 6000.0
NSSF_UEL = 18000.0
NSSF_RATE = 0.06

def calc_nssf(gross: float) -> float:
    """Kenya NSSF Act 2013 tiered contribution (employee share)."""
    tier1 = round(min(gross, NSSF_LEL) * NSSF_RATE, 2)
    tier2_base = max(0.0, min(gross, NSSF_UEL) - NSSF_LEL)
    tier2 = round(tier2_base * NSSF_RATE, 2)
    return round(tier1 + tier2, 2)

def calc_nssf_employer(gross: float) -> float:
    """Employer NSSF contribution mirrors employee."""
    return calc_nssf(gross)

HOUSING_LEVY_RATE = 0.015  # 1.5% of gross

def calc_housing_levy(gross: float) -> float:
    return round(gross * HOUSING_LEVY_RATE, 2)

# ─────────────────────────────────────────────────────────────────────────────
# Models
# ─────────────────────────────────────────────────────────────────────────────

class StaffPayConfig(BaseModel):
    user_id: int
    pay_type: str          # monthly | hourly | daily
    basic_salary: float    # monthly salary OR hourly/daily rate
    house_allowance: Optional[float] = 0
    transport_allowance: Optional[float] = 0
    airtime_allowance: Optional[float] = 0
    other_allowances: Optional[float] = 0
    other_allowance_label: Optional[str] = ""
    # Deductions
    loan_deduction: Optional[float] = 0
    sacco_deduction: Optional[float] = 0
    advance_deduction: Optional[float] = 0
    other_deductions: Optional[float] = 0
    other_deduction_label: Optional[str] = ""
    # Flags
    nssf_exempt: Optional[bool] = False
    nhif_exempt: Optional[bool] = False
    housing_levy_exempt: Optional[bool] = False
    notes: Optional[str] = ""

class PayrollRunCreate(BaseModel):
    period: str            # YYYY-MM
    notes: Optional[str] = ""
    overrides: Optional[List[dict]] = None  # [{user_id, hours_worked, days_worked, bonus}]

class PayslipApprove(BaseModel):
    payslip_ids: List[int]
    action: str            # approve | reject | pay

# ─────────────────────────────────────────────────────────────────────────────
# Calculation engine
# ─────────────────────────────────────────────────────────────────────────────

def compute_payslip(config: dict, hours_worked: float = 0,
                    days_worked: float = 0, bonus: float = 0,
                    period: str = "") -> dict:
    """
    Compute full payslip for one employee.
    Returns a dict with all earnings, deductions, and net pay.
    """
    pay_type = config.get("pay_type", "monthly")

    # ── Gross earnings ──────────────────────────────────────────
    if pay_type == "monthly":
        basic = config.get("basic_salary", 0)
    elif pay_type == "hourly":
        basic = config.get("basic_salary", 0) * hours_worked
    elif pay_type == "daily":
        basic = config.get("basic_salary", 0) * days_worked
    else:
        basic = config.get("basic_salary", 0)

    house_allow   = config.get("house_allowance", 0) or 0
    transport_allow = config.get("transport_allowance", 0) or 0
    airtime_allow = config.get("airtime_allowance", 0) or 0
    other_allow   = config.get("other_allowances", 0) or 0
    bonus_amt     = bonus or 0

    gross = basic + house_allow + transport_allow + airtime_allow + other_allow + bonus_amt

    # ── Statutory deductions ────────────────────────────────────
    nssf     = 0.0 if config.get("nssf_exempt") else calc_nssf(gross)
    nhif     = 0.0 if config.get("nhif_exempt") else calc_nhif(gross)
    housing  = 0.0 if config.get("housing_levy_exempt") else calc_housing_levy(gross)

    # Taxable = gross - NSSF - housing levy (per KRA rules)
    taxable  = max(0.0, gross - nssf - housing)
    paye     = calc_paye(taxable)

    total_statutory = nssf + nhif + housing + paye

    # ── Voluntary deductions ────────────────────────────────────
    loan_ded   = config.get("loan_deduction", 0) or 0
    sacco_ded  = config.get("sacco_deduction", 0) or 0
    advance_ded = config.get("advance_deduction", 0) or 0
    other_ded  = config.get("other_deductions", 0) or 0
    total_voluntary = loan_ded + sacco_ded + advance_ded + other_ded

    # ── Net pay ─────────────────────────────────────────────────
    net_pay = round(gross - total_statutory - total_voluntary, 2)

    return {
        "period":            period,
        "pay_type":          pay_type,
        "hours_worked":      round(hours_worked, 2),
        "days_worked":       round(days_worked, 2),
        "basic_salary":      round(basic, 2),
        "house_allowance":   round(house_allow, 2),
        "transport_allowance": round(transport_allow, 2),
        "airtime_allowance": round(airtime_allow, 2),
        "other_allowances":  round(other_allow, 2),
        "other_allowance_label": config.get("other_allowance_label", ""),
        "bonus":             round(bonus_amt, 2),
        "gross_pay":         round(gross, 2),
        # Statutory
        "nssf_employee":     round(nssf, 2),
        "nhif":              round(nhif, 2),
        "housing_levy":      round(housing, 2),
        "paye":              round(paye, 2),
        "total_statutory":   round(total_statutory, 2),
        # Voluntary
        "loan_deduction":    round(loan_ded, 2),
        "sacco_deduction":   round(sacco_ded, 2),
        "advance_deduction": round(advance_ded, 2),
        "other_deductions":  round(other_ded, 2),
        "other_deduction_label": config.get("other_deduction_label", ""),
        "total_voluntary":   round(total_voluntary, 2),
        # Employer costs
        "nssf_employer":     round(calc_nssf_employer(gross), 2),
        "employer_total_cost": round(gross + calc_nssf_employer(gross), 2),
        # Net
        "net_pay":           net_pay,
    }

# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/configs")
async def list_configs(current_user=Depends(get_current_user)):
    """All staff pay configurations."""
    if current_user.get("role") not in ["admin", "manager"]:
        raise HTTPException(403, "Manager or Admin required")
    db = get_db()
    rows = db.execute("""
        SELECT pc.*, u.full_name, u.role AS staff_role, u.username
        FROM payroll_configs pc
        JOIN users u ON pc.user_id = u.id
        WHERE u.is_active = 1
        ORDER BY u.full_name
    """).fetchall()
    db.close()
    return [dict(r) for r in rows]

@router.get("/configs/unconfigured")
async def unconfigured_staff(current_user=Depends(get_current_user)):
    """Staff members who don't have a pay config yet."""
    if current_user.get("role") not in ["admin", "manager"]:
        raise HTTPException(403, "Manager or Admin required")
    db = get_db()
    rows = db.execute("""
        SELECT u.id, u.full_name, u.role, u.username
        FROM users u
        LEFT JOIN payroll_configs pc ON pc.user_id = u.id
        WHERE u.is_active = 1 AND pc.id IS NULL
        ORDER BY u.full_name
    """).fetchall()
    db.close()
    return [dict(r) for r in rows]

@router.post("/configs")
async def save_config(data: StaffPayConfig, current_user=Depends(get_current_user)):
    """Create or update a staff pay configuration."""
    if current_user.get("role") not in ["admin", "manager"]:
        raise HTTPException(403, "Manager or Admin required")
    db = get_db()
    existing = db.execute("SELECT id FROM payroll_configs WHERE user_id=?", (data.user_id,)).fetchone()
    fields = dict(data)
    fields["nssf_exempt"]         = int(data.nssf_exempt or False)
    fields["nhif_exempt"]         = int(data.nhif_exempt or False)
    fields["housing_levy_exempt"] = int(data.housing_levy_exempt or False)

    if existing:
        sets = ",".join(f"{k}=?" for k in fields if k != "user_id")
        vals = [fields[k] for k in fields if k != "user_id"] + [data.user_id]
        db.execute(f"UPDATE payroll_configs SET {sets} WHERE user_id=?", vals)
    else:
        cols = ",".join(fields.keys())
        qs   = ",".join("?" * len(fields))
        db.execute(f"INSERT INTO payroll_configs ({cols}) VALUES ({qs})", list(fields.values()))
    db.commit(); db.close()
    return {"saved": True}

@router.delete("/configs/{user_id}")
async def delete_config(user_id: int, current_user=Depends(get_current_user)):
    if current_user.get("role") != "admin":
        raise HTTPException(403, "Admin required")
    db = get_db()
    db.execute("DELETE FROM payroll_configs WHERE user_id=?", (user_id,))
    db.commit(); db.close()
    return {"deleted": True}

@router.post("/preview")
async def preview_payroll(data: PayrollRunCreate, current_user=Depends(get_current_user)):
    """Preview payroll for a period without saving."""
    if current_user.get("role") not in ["admin", "manager"]:
        raise HTTPException(403, "Manager or Admin required")
    db = get_db()

    # Load all configured staff
    configs = db.execute("""
        SELECT pc.*, u.full_name, u.role AS staff_role, u.username
        FROM payroll_configs pc JOIN users u ON pc.user_id = u.id
        WHERE u.is_active = 1
    """).fetchall()

    # Hours/days worked from shifts
    period = data.period
    y, m = period.split("-")
    shifts = db.execute("""
        SELECT user_id, SUM(hours_worked) as total_hours,
               COUNT(DISTINCT shift_date) as days_worked
        FROM shifts
        WHERE strftime('%Y-%m', shift_date) = ?
        GROUP BY user_id
    """, (period,)).fetchall()
    shift_map = {r[0]: {"hours": r[1] or 0, "days": r[2] or 0} for r in shifts}

    # Overrides
    override_map = {o["user_id"]: o for o in (data.overrides or [])}
    db.close()

    payslips = []
    total_gross = total_net = total_paye = total_nssf = 0

    for cfg in configs:
        c = dict(cfg)
        uid = c["user_id"]
        shift_data = shift_map.get(uid, {"hours": 0, "days": 0})
        override = override_map.get(uid, {})
        hours = override.get("hours_worked", shift_data["hours"])
        days  = override.get("days_worked",  shift_data["days"])
        bonus = override.get("bonus", 0)

        slip = compute_payslip(c, hours_worked=hours, days_worked=days,
                               bonus=bonus, period=period)
        slip.update({
            "user_id":    uid,
            "full_name":  c["full_name"],
            "staff_role": c["staff_role"],
            "username":   c["username"],
        })
        payslips.append(slip)
        total_gross += slip["gross_pay"]
        total_net   += slip["net_pay"]
        total_paye  += slip["paye"]
        total_nssf  += slip["nssf_employee"]

    return {
        "period":       period,
        "staff_count":  len(payslips),
        "total_gross":  round(total_gross, 2),
        "total_net":    round(total_net, 2),
        "total_paye":   round(total_paye, 2),
        "total_nssf":   round(total_nssf, 2),
        "payslips":     payslips,
    }

@router.post("/run")
async def run_payroll(data: PayrollRunCreate, current_user=Depends(get_current_user)):
    """
    Process payroll for a period:
    1. Compute all payslips
    2. Save payroll_runs record
    3. Save individual payslips
    4. Post total to expenses (category=Payroll)
    """
    if current_user.get("role") not in ["admin", "manager"]:
        raise HTTPException(403, "Manager or Admin required")

    db = get_db()

    # Check for duplicate run
    existing_run = db.execute(
        "SELECT id FROM payroll_runs WHERE period=? AND status != 'cancelled'",
        (data.period,)
    ).fetchone()
    if existing_run:
        db.close()
        raise HTTPException(400, f"Payroll for {data.period} already exists (ID {existing_run[0]})")

    # Load configs and compute
    configs = db.execute("""
        SELECT pc.*, u.full_name, u.role AS staff_role, u.username
        FROM payroll_configs pc JOIN users u ON pc.user_id = u.id
        WHERE u.is_active = 1
    """).fetchall()
    if not configs:
        db.close()
        raise HTTPException(400, "No staff pay configurations found. Set up pay configs first.")

    period = data.period
    shifts = db.execute("""
        SELECT user_id, SUM(hours_worked), COUNT(DISTINCT shift_date)
        FROM shifts WHERE strftime('%Y-%m', shift_date)=? GROUP BY user_id
    """, (period,)).fetchall()
    shift_map    = {r[0]: {"hours": r[1] or 0, "days": r[2] or 0} for r in shifts}
    override_map = {o["user_id"]: o for o in (data.overrides or [])}

    uid = current_user.get("user_id") or current_user.get("id")
    total_gross = total_net = total_paye = total_nssf = total_nhif = 0

    # Create payroll run header
    db.execute("""
        INSERT INTO payroll_runs (period, status, notes, created_by)
        VALUES (?, 'draft', ?, ?)
    """, (period, data.notes or "", uid))
    run_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]

    # Save each payslip
    for cfg in configs:
        c = dict(cfg)
        emp_id   = c["user_id"]
        override = override_map.get(emp_id, {})
        shift_d  = shift_map.get(emp_id, {"hours": 0, "days": 0})
        hours    = override.get("hours_worked", shift_d["hours"])
        days     = override.get("days_worked",  shift_d["days"])
        bonus    = override.get("bonus", 0)

        slip = compute_payslip(c, hours_worked=hours, days_worked=days,
                               bonus=bonus, period=period)
        slip_json = json.dumps(slip)

        db.execute("""
            INSERT INTO payroll_payslips
            (run_id, user_id, period, full_name, gross_pay, net_pay,
             paye, nssf_employee, nhif, housing_levy,
             total_statutory, total_voluntary, payslip_data, status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'{}','draft')
        """.format(slip_json), (
            run_id, emp_id, period, c["full_name"],
            slip["gross_pay"], slip["net_pay"],
            slip["paye"], slip["nssf_employee"], slip["nhif"],
            slip["housing_levy"], slip["total_statutory"],
            slip["total_voluntary"]
        ))

        total_gross += slip["gross_pay"]
        total_net   += slip["net_pay"]
        total_paye  += slip["paye"]
        total_nssf  += slip["nssf_employee"]
        total_nhif  += slip["nhif"]

    # Update run totals
    db.execute("""
        UPDATE payroll_runs
        SET total_gross=?, total_net=?, total_paye=?, total_nssf=?, total_nhif=?,
            staff_count=?
        WHERE id=?
    """, (round(total_gross,2), round(total_net,2), round(total_paye,2),
          round(total_nssf,2), round(total_nhif,2), len(configs), run_id))

    db.commit(); db.close()
    return {
        "success":     True,
        "run_id":      run_id,
        "period":      period,
        "staff_count": len(configs),
        "total_gross": round(total_gross, 2),
        "total_net":   round(total_net, 2),
    }

@router.get("/runs")
async def list_runs(current_user=Depends(get_current_user)):
    if current_user.get("role") not in ["admin", "manager"]:
        raise HTTPException(403, "Manager or Admin required")
    db = get_db()
    rows = db.execute("""
        SELECT pr.*, u.full_name as created_by_name
        FROM payroll_runs pr
        LEFT JOIN users u ON pr.created_by = u.id
        ORDER BY pr.period DESC
    """).fetchall()
    db.close()
    return [dict(r) for r in rows]

@router.get("/runs/{run_id}")
async def get_run(run_id: int, current_user=Depends(get_current_user)):
    if current_user.get("role") not in ["admin", "manager"]:
        raise HTTPException(403, "Manager or Admin required")
    db = get_db()
    run = db.execute("SELECT * FROM payroll_runs WHERE id=?", (run_id,)).fetchone()
    if not run: db.close(); raise HTTPException(404, "Payroll run not found")
    slips = db.execute(
        "SELECT * FROM payroll_payslips WHERE run_id=? ORDER BY full_name", (run_id,)
    ).fetchall()
    db.close()
    result = dict(run)
    result["payslips"] = []
    for s in slips:
        sd = dict(s)
        try: sd["details"] = json.loads(sd.get("payslip_data","{}"))
        except: sd["details"] = {}
        result["payslips"].append(sd)
    return result

@router.post("/runs/{run_id}/approve")
async def approve_run(run_id: int, body: dict, current_user=Depends(get_current_user)):
    """Approve payroll run → posts to expenses, marks as paid."""
    if current_user.get("role") != "admin":
        raise HTTPException(403, "Admin only")
    db = get_db()
    run = db.execute("SELECT * FROM payroll_runs WHERE id=?", (run_id,)).fetchone()
    if not run: db.close(); raise HTTPException(404, "Run not found")
    if run["status"] == "paid":
        db.close(); raise HTTPException(400, "Already paid")

    payment_date = body.get("payment_date") or datetime.date.today().isoformat()
    uid = current_user.get("user_id") or current_user.get("id")

    # Post to expenses
    db.execute("""
        INSERT INTO expenses (title, amount, category, description, date,
                              payment_method, reference, recorded_by)
        VALUES (?,?,?,?,?,?,?,?)
    """, (
        f"Payroll — {run['period']}",
        run["total_net"],
        "Payroll",
        f"Net pay for {run['staff_count']} staff. PAYE: KES {run['total_paye']:,.0f}, "
        f"NSSF: KES {run['total_nssf']:,.0f}",
        payment_date,
        body.get("payment_method", "bank_transfer"),
        f"PR-{run_id:05d}",
        uid
    ))

    # Also post PAYE as a separate expense (paid to KRA)
    if run["total_paye"] > 0:
        db.execute("""
            INSERT INTO expenses (title, amount, category, description, date,
                                  payment_method, reference, recorded_by)
            VALUES (?,?,?,?,?,?,?,?)
        """, (
            f"PAYE — {run['period']}",
            run["total_paye"],
            "Tax",
            f"PAYE remittance for {run['period']}",
            payment_date,
            "bank_transfer",
            f"PAYE-{run_id:05d}",
            uid
        ))

    # Update run and payslips
    db.execute("""
        UPDATE payroll_runs SET status='paid', payment_date=?, approved_by=?
        WHERE id=?
    """, (payment_date, uid, run_id))
    db.execute(
        "UPDATE payroll_payslips SET status='paid', payment_date=? WHERE run_id=?",
        (payment_date, run_id)
    )
    db.commit(); db.close()
    return {"approved": True, "payment_date": payment_date}

@router.delete("/runs/{run_id}")
async def cancel_run(run_id: int, current_user=Depends(get_current_user)):
    if current_user.get("role") != "admin":
        raise HTTPException(403, "Admin only")
    db = get_db()
    db.execute("UPDATE payroll_runs SET status='cancelled' WHERE id=?", (run_id,))
    db.execute("UPDATE payroll_payslips SET status='cancelled' WHERE run_id=?", (run_id,))
    db.commit(); db.close()
    return {"cancelled": True}

@router.get("/history/{user_id}")
async def employee_pay_history(user_id: int, current_user=Depends(get_current_user)):
    if current_user.get("role") not in ["admin", "manager"]:
        raise HTTPException(403, "Manager or Admin required")
    db = get_db()
    rows = db.execute("""
        SELECT ps.*, pr.payment_date, pr.status as run_status
        FROM payroll_payslips ps
        JOIN payroll_runs pr ON ps.run_id = pr.id
        WHERE ps.user_id = ? ORDER BY ps.period DESC LIMIT 24
    """, (user_id,)).fetchall()
    db.close()
    return [dict(r) for r in rows]

@router.get("/payslip/{payslip_id}/print", response_class=Response)
async def print_payslip(payslip_id: int, current_user=Depends(get_current_user)):
    """Returns a printable HTML payslip."""
    db = get_db()
    ps = db.execute("""
        SELECT ps.*, pr.payment_date, u.full_name, u.username
        FROM payroll_payslips ps
        JOIN payroll_runs pr ON ps.run_id = pr.id
        JOIN users u ON ps.user_id = u.id
        WHERE ps.id=?
    """, (payslip_id,)).fetchone()
    if not ps: db.close(); raise HTTPException(404, "Payslip not found")
    d = dict(ps)
    try: details = json.loads(d.get("payslip_data","{}"))
    except: details = {}

    biz = db.execute("SELECT value FROM settings WHERE key='business_name'").fetchone()
    biz_name = biz[0] if biz else "Neural POS"
    biz_phone_row = db.execute("SELECT value FROM settings WHERE key='business_phone'").fetchone()
    biz_phone = biz_phone_row[0] if biz_phone_row else ""
    db.close()

    fmt = lambda v: f"KES {float(v or 0):,.2f}"
    period_label = d.get("period","")

    html = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Payslip — {d['full_name']} {period_label}</title>
<style>
  body {{ font-family: Arial, sans-serif; font-size: 11pt; color: #111; padding: 18mm; }}
  .header {{ display:flex; justify-content:space-between; margin-bottom:16px; }}
  h1 {{ font-size:20pt; color:#2d1b69; margin:0 }}
  .meta {{ text-align:right; font-size:9pt; color:#555; }}
  .emp-box {{ background:#f5f0ff; border-radius:6px; padding:12px 16px; margin-bottom:16px; display:flex; justify-content:space-between; }}
  .section-title {{ font-size:9pt; text-transform:uppercase; letter-spacing:1px; color:#2d1b69; font-weight:bold; margin:14px 0 6px; border-bottom:2px solid #2d1b69; padding-bottom:3px; }}
  table {{ width:100%; border-collapse:collapse; }}
  td {{ padding:5px 6px; font-size:10pt; }}
  .td-label {{ color:#555; width:60%; }}
  .td-amount {{ text-align:right; font-family:monospace; }}
  .td-amount.earning {{ color:#155724; }}
  .td-amount.deduction {{ color:#842029; }}
  .total-row td {{ font-weight:bold; font-size:11pt; border-top:2px solid #2d1b69; padding-top:8px; }}
  .net-box {{ background:#2d1b69; color:#fff; border-radius:8px; padding:14px 20px; margin-top:16px; display:flex; justify-content:space-between; align-items:center; }}
  .net-label {{ font-size:12pt; }}
  .net-amount {{ font-size:18pt; font-family:monospace; }}
  .footer {{ margin-top:24px; font-size:8pt; color:#888; border-top:1px solid #eee; padding-top:8px; text-align:center; }}
  @media print {{ .no-print {{ display:none; }} }}
</style></head>
<body>
<div class="no-print" style="background:#2d1b69;color:#fff;padding:10px 20px;margin:-18mm -18mm 14px;display:flex;justify-content:space-between">
  <span>Payslip — Neural POS</span>
  <button onclick="window.print()" style="background:#fff;color:#2d1b69;border:none;padding:5px 14px;border-radius:4px;cursor:pointer;font-weight:bold">🖨 Print</button>
</div>
<div class="header">
  <div><h1>{biz_name}</h1><div style="color:#888;font-size:9pt">{biz_phone}</div></div>
  <div class="meta">
    <div style="font-size:14pt;font-weight:bold;color:#2d1b69">PAYSLIP</div>
    <div>Period: <strong>{period_label}</strong></div>
    <div>Payment Date: {d.get('payment_date') or '—'}</div>
    <div>Ref: PS-{payslip_id:05d}</div>
  </div>
</div>

<div class="emp-box">
  <div>
    <div style="font-size:13pt;font-weight:bold">{d['full_name']}</div>
    <div style="color:#555;font-size:9pt">{d.get('staff_role','').upper()} &nbsp;·&nbsp; @{d['username']}</div>
  </div>
  <div style="text-align:right;font-size:9pt;color:#555">
    <div>Pay type: {details.get('pay_type','monthly').title()}</div>
    {"<div>Hours: "+str(details.get('hours_worked',0))+"</div>" if details.get('hours_worked') else ""}
    {"<div>Days: "+str(details.get('days_worked',0))+"</div>" if details.get('days_worked') else ""}
  </div>
</div>

<div class="section-title">Earnings</div>
<table>
  <tr><td class="td-label">Basic Salary / Wage</td><td class="td-amount earning">{fmt(details.get('basic_salary',0))}</td></tr>
  {"<tr><td class='td-label'>House Allowance</td><td class='td-amount earning'>"+fmt(details.get('house_allowance',0))+"</td></tr>" if details.get('house_allowance') else ""}
  {"<tr><td class='td-label'>Transport Allowance</td><td class='td-amount earning'>"+fmt(details.get('transport_allowance',0))+"</td></tr>" if details.get('transport_allowance') else ""}
  {"<tr><td class='td-label'>Airtime Allowance</td><td class='td-amount earning'>"+fmt(details.get('airtime_allowance',0))+"</td></tr>" if details.get('airtime_allowance') else ""}
  {"<tr><td class='td-label'>"+(details.get('other_allowance_label') or 'Other Allowance')+"</td><td class='td-amount earning'>"+fmt(details.get('other_allowances',0))+"</td></tr>" if details.get('other_allowances') else ""}
  {"<tr><td class='td-label'>Bonus</td><td class='td-amount earning'>"+fmt(details.get('bonus',0))+"</td></tr>" if details.get('bonus') else ""}
  <tr class="total-row"><td class="td-label">Gross Pay</td><td class="td-amount earning">{fmt(details.get('gross_pay',0))}</td></tr>
</table>

<div class="section-title">Statutory Deductions</div>
<table>
  <tr><td class="td-label">PAYE Tax</td><td class="td-amount deduction">{fmt(details.get('paye',0))}</td></tr>
  <tr><td class="td-label">NSSF (Employee)</td><td class="td-amount deduction">{fmt(details.get('nssf_employee',0))}</td></tr>
  <tr><td class="td-label">NHIF / SHA</td><td class="td-amount deduction">{fmt(details.get('nhif',0))}</td></tr>
  <tr><td class="td-label">Housing Levy (1.5%)</td><td class="td-amount deduction">{fmt(details.get('housing_levy',0))}</td></tr>
  <tr class="total-row"><td class="td-label">Total Statutory</td><td class="td-amount deduction">{fmt(details.get('total_statutory',0))}</td></tr>
</table>

{"" if not (details.get('loan_deduction') or details.get('sacco_deduction') or details.get('advance_deduction') or details.get('other_deductions')) else f'''
<div class="section-title">Other Deductions</div>
<table>
  {"<tr><td class='td-label'>Loan Repayment</td><td class='td-amount deduction'>"+fmt(details.get('loan_deduction',0))+"</td></tr>" if details.get('loan_deduction') else ""}
  {"<tr><td class='td-label'>SACCO</td><td class='td-amount deduction'>"+fmt(details.get('sacco_deduction',0))+"</td></tr>" if details.get('sacco_deduction') else ""}
  {"<tr><td class='td-label'>Salary Advance</td><td class='td-amount deduction'>"+fmt(details.get('advance_deduction',0))+"</td></tr>" if details.get('advance_deduction') else ""}
  {"<tr><td class='td-label'>"+(details.get('other_deduction_label') or 'Other')+"</td><td class='td-amount deduction'>"+fmt(details.get('other_deductions',0))+"</td></tr>" if details.get('other_deductions') else ""}
  <tr class="total-row"><td class="td-label">Total Other</td><td class="td-amount deduction">{fmt(details.get('total_voluntary',0))}</td></tr>
</table>
'''}

<div class="net-box">
  <div class="net-label">NET PAY</div>
  <div class="net-amount">{fmt(details.get('net_pay', d.get('net_pay',0)))}</div>
</div>

<div class="footer">
  This is a computer-generated payslip. {biz_name} &nbsp;·&nbsp; Generated {datetime.date.today().strftime('%d %B %Y')}
</div>
</body></html>"""
    return Response(content=html, media_type="text/html")

@router.get("/summary")
async def payroll_summary(current_user=Depends(get_current_user)):
    if current_user.get("role") not in ["admin", "manager"]:
        raise HTTPException(403, "Manager or Admin required")
    db = get_db()
    total_staff   = db.execute("SELECT COUNT(*) FROM payroll_configs pc JOIN users u ON pc.user_id=u.id WHERE u.is_active=1").fetchone()[0]
    last_run      = db.execute("SELECT period, total_gross, total_net, status FROM payroll_runs ORDER BY period DESC LIMIT 1").fetchone()
    ytd_expense   = db.execute("SELECT COALESCE(SUM(amount),0) FROM expenses WHERE category='Payroll' AND strftime('%Y',date)=strftime('%Y','now')").fetchone()[0]
    pending_runs  = db.execute("SELECT COUNT(*) FROM payroll_runs WHERE status='draft'").fetchone()[0]
    db.close()
    return {
        "configured_staff": total_staff,
        "last_run": dict(last_run) if last_run else None,
        "ytd_payroll_expense": ytd_expense,
        "pending_runs": pending_runs,
    }
