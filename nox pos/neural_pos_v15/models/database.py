"""
Neural POS — Database (production hardened)
- bcrypt direct (no passlib — compatible with bcrypt 4.x and Python 3.14)
- DB indexes on high-traffic columns
- Automated backup helper
"""
import sqlite3, os, shutil, datetime

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── Paths — always resolve relative to THIS file, immune to cwd ──────────────
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))   # .../neural_pos/models/
_ROOT_DIR = os.path.dirname(_THIS_DIR)                   # .../neural_pos/

DB_PATH    = os.path.join(_ROOT_DIR, "data", "neural_pos.db")
BACKUP_DIR = os.path.join(_ROOT_DIR, "data", "backups")

def get_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")   # Better concurrent read performance
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

# ── Password hashing — direct bcrypt (works with bcrypt 4.x + Python 3.14) ──
# passlib is NOT used — it is incompatible with bcrypt 4.x on Python 3.14
try:
    import bcrypt as _bcrypt

    def hash_password(pw: str) -> str:
        return _bcrypt.hashpw(pw.encode("utf-8"), _bcrypt.gensalt()).decode("utf-8")

    def verify_password(pw: str, hashed: str) -> bool:
        # Legacy SHA-256 hashes are exactly 64 hex chars
        if len(hashed) == 64 and all(c in "0123456789abcdef" for c in hashed):
            import hashlib
            return hashlib.sha256(pw.encode()).hexdigest() == hashed
        try:
            return _bcrypt.checkpw(pw.encode("utf-8"), hashed.encode("utf-8"))
        except Exception:
            return False

except ImportError:
    # Fallback: SHA-256 (safe for dev, not for production)
    import hashlib

    def hash_password(pw: str) -> str:
        return hashlib.sha256(pw.encode()).hexdigest()

    def verify_password(pw: str, hashed: str) -> bool:
        return hash_password(pw) == hashed

# ── Backup ────────────────────────────────────────────────────────────────────
def backup_db() -> str:
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts   = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = os.path.join(BACKUP_DIR, f"neural_pos_{ts}.db")
    shutil.copy2(DB_PATH, dest)
    # Keep only last 30 backups
    backups = sorted(
        [f for f in os.listdir(BACKUP_DIR) if f.endswith(".db")],
        reverse=True
    )
    for old in backups[30:]:
        try: os.remove(os.path.join(BACKUP_DIR, old))
        except: pass
    return dest

# ── Schema ────────────────────────────────────────────────────────────────────
def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            full_name     TEXT NOT NULL,
            role          TEXT NOT NULL DEFAULT 'cashier',
            email         TEXT,
            phone         TEXT,
            pin_hash      TEXT,
            is_active     INTEGER DEFAULT 1,
            last_login    TEXT,
            created_at    TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            sku            TEXT UNIQUE NOT NULL,
            name           TEXT NOT NULL,
            description    TEXT,
            category       TEXT,
            price          REAL NOT NULL,
            cost           REAL DEFAULT 0,
            stock_quantity INTEGER DEFAULT 0,
            reorder_level  INTEGER DEFAULT 10,
            supplier_id    INTEGER,
            barcode        TEXT,
            expiry_date    TEXT,
            is_active      INTEGER DEFAULT 1,
            created_at     TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at     TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (supplier_id) REFERENCES suppliers(id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS suppliers (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            name           TEXT NOT NULL,
            contact_person TEXT,
            email          TEXT,
            phone          TEXT,
            address        TEXT,
            city           TEXT,
            country        TEXT DEFAULT 'Kenya',
            payment_terms  TEXT DEFAULT 'Net 30',
            lead_time_days INTEGER DEFAULT 7,
            notes          TEXT DEFAULT '',
            bank_account   TEXT DEFAULT '',
            mpesa_till     TEXT DEFAULT '',
            total_orders   INTEGER DEFAULT 0,
            total_spent    REAL DEFAULT 0,
            is_active      INTEGER DEFAULT 1,
            created_at     TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            order_number   TEXT UNIQUE NOT NULL,
            cashier_id     INTEGER,
            customer_name  TEXT,
            customer_email TEXT,
            subtotal       REAL DEFAULT 0,
            tax            REAL DEFAULT 0,
            discount       REAL DEFAULT 0,
            total          REAL NOT NULL,
            payment_method TEXT DEFAULT 'cash',
            status         TEXT DEFAULT 'completed',
            notes          TEXT,
            created_at     TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (cashier_id) REFERENCES users(id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS order_items (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id     INTEGER NOT NULL,
            product_id   INTEGER,
            product_name TEXT NOT NULL,
            quantity     INTEGER NOT NULL,
            unit_price   REAL NOT NULL,
            total_price  REAL NOT NULL,
            FOREIGN KEY (order_id)   REFERENCES orders(id),
            FOREIGN KEY (product_id) REFERENCES products(id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS purchase_orders (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            po_number      TEXT UNIQUE NOT NULL,
            supplier_id    INTEGER,
            total_amount   REAL DEFAULT 0,
            status         TEXT DEFAULT 'pending',
            expected_date  TEXT,
            received_date  TEXT,
            notes          TEXT,
            created_by     INTEGER,
            created_at     TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (supplier_id) REFERENCES suppliers(id),
            FOREIGN KEY (created_by)  REFERENCES users(id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS invoices (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_number TEXT UNIQUE NOT NULL,
            client_name    TEXT NOT NULL,
            client_email   TEXT,
            client_address TEXT,
            issue_date     TEXT,
            due_date       TEXT,
            subtotal       REAL DEFAULT 0,
            tax_rate       REAL DEFAULT 16,
            tax_amount     REAL DEFAULT 0,
            total          REAL DEFAULT 0,
            status         TEXT DEFAULT 'draft',
            notes          TEXT,
            created_by     INTEGER,
            created_at     TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (created_by) REFERENCES users(id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS invoice_items (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_id  INTEGER NOT NULL,
            description TEXT NOT NULL,
            quantity    REAL DEFAULT 1,
            unit_price  REAL NOT NULL,
            total_price REAL NOT NULL,
            FOREIGN KEY (invoice_id) REFERENCES invoices(id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS customers (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            name           TEXT NOT NULL,
            phone          TEXT,
            email          TEXT,
            address        TEXT,
            notes          TEXT,
            loyalty_points INTEGER DEFAULT 0,
            credit_limit   REAL DEFAULT 0,
            credit_balance REAL DEFAULT 0,
            total_spent    REAL DEFAULT 0,
            total_orders   INTEGER DEFAULT 0,
            is_active      INTEGER DEFAULT 1,
            created_at     TEXT DEFAULT CURRENT_TIMESTAMP,
            last_purchase  TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS expenses (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            title          TEXT NOT NULL,
            amount         REAL NOT NULL,
            category       TEXT DEFAULT 'Other',
            date           TEXT NOT NULL,
            payment_method TEXT DEFAULT 'cash',
            reference      TEXT,
            description    TEXT,
            recorded_by    INTEGER,
            created_at     TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (recorded_by) REFERENCES users(id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS staff_shifts (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL,
            shift_date TEXT NOT NULL,
            start_time TEXT NOT NULL,
            end_time   TEXT,
            hours      REAL DEFAULT 0,
            notes      TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS returns (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id       INTEGER,
            order_number   TEXT,
            reason         TEXT,
            refund_amount  REAL DEFAULT 0,
            refund_method  TEXT DEFAULT 'cash',
            status         TEXT DEFAULT 'processed',
            processed_by   INTEGER,
            notes          TEXT,
            created_at     TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (order_id)      REFERENCES orders(id),
            FOREIGN KEY (processed_by)  REFERENCES users(id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS credit_transactions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER,
            type        TEXT,
            amount      REAL,
            description TEXT,
            created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (customer_id) REFERENCES customers(id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key        TEXT PRIMARY KEY,
            value      TEXT DEFAULT '',
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS access_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER,
            username   TEXT NOT NULL,
            full_name  TEXT DEFAULT '',
            role       TEXT DEFAULT '',
            action     TEXT NOT NULL,
            detail     TEXT DEFAULT '',
            ip_address TEXT DEFAULT '',
            user_agent TEXT DEFAULT '',
            status     TEXT DEFAULT 'success',
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)

    # ── Extended tables (previously scattered across routers) ────────────────
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS product_variants (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id       INTEGER NOT NULL,
            name             TEXT NOT NULL,
            sku              TEXT DEFAULT '',
            barcode          TEXT DEFAULT '',
            price_adjustment REAL DEFAULT 0,
            stock_quantity   INTEGER DEFAULT 0,
            attributes       TEXT DEFAULT '',
            is_active        INTEGER DEFAULT 1,
            created_at       TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (product_id) REFERENCES products(id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS mpesa_transactions (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            checkout_request_id TEXT UNIQUE NOT NULL,
            merchant_request_id TEXT DEFAULT '',
            phone               TEXT NOT NULL,
            amount              REAL NOT NULL,
            order_ref           TEXT DEFAULT '',
            account_ref         TEXT DEFAULT '',
            status              TEXT DEFAULT 'pending',
            mpesa_receipt       TEXT DEFAULT '',
            result_code         TEXT DEFAULT '',
            result_desc         TEXT DEFAULT '',
            created_at          TEXT DEFAULT (datetime('now')),
            updated_at          TEXT DEFAULT (datetime('now'))
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS notifications (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            type       TEXT NOT NULL,
            title      TEXT NOT NULL,
            message    TEXT DEFAULT '',
            severity   TEXT DEFAULT 'info',
            is_read    INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS promotions (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            name         TEXT NOT NULL,
            type         TEXT NOT NULL,
            value        REAL DEFAULT 0,
            buy_qty      INTEGER DEFAULT 1,
            get_qty      INTEGER DEFAULT 1,
            category     TEXT DEFAULT '',
            product_id   INTEGER,
            code         TEXT DEFAULT '',
            min_order    REAL DEFAULT 0,
            start_date   TEXT DEFAULT '',
            end_date     TEXT DEFAULT '',
            is_active    INTEGER DEFAULT 1,
            uses_count   INTEGER DEFAULT 0,
            created_at   TEXT DEFAULT (datetime('now'))
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS return_items (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            return_id   INTEGER NOT NULL,
            product_id  INTEGER,
            product_name TEXT NOT NULL,
            quantity    INTEGER DEFAULT 1,
            unit_price  REAL DEFAULT 0,
            FOREIGN KEY (return_id) REFERENCES returns(id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sms_log (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            phone        TEXT NOT NULL,
            message      TEXT NOT NULL,
            channel      TEXT DEFAULT 'sms',
            order_ref    TEXT DEFAULT '',
            status       TEXT DEFAULT 'sent',
            provider_ref TEXT DEFAULT '',
            created_at   TEXT DEFAULT (datetime('now'))
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS po_items (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            po_id             INTEGER NOT NULL,
            product_id        INTEGER NOT NULL,
            product_name      TEXT NOT NULL,
            quantity_ordered  INTEGER NOT NULL,
            quantity_received INTEGER DEFAULT 0,
            unit_cost         REAL NOT NULL,
            total_cost        REAL NOT NULL,
            notes             TEXT DEFAULT '',
            FOREIGN KEY (po_id) REFERENCES purchase_orders(id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS po_receive_log (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            po_id             INTEGER NOT NULL,
            product_id        INTEGER NOT NULL,
            product_name      TEXT NOT NULL,
            quantity_received INTEGER NOT NULL,
            received_by       INTEGER,
            received_at       TEXT DEFAULT (datetime('now')),
            notes             TEXT DEFAULT '',
            FOREIGN KEY (po_id) REFERENCES purchase_orders(id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS cash_ups (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            shift_date    TEXT NOT NULL,
            cashier_id    INTEGER,
            cashier_name  TEXT DEFAULT '',
            expected_cash REAL DEFAULT 0,
            counted_cash  REAL DEFAULT 0,
            float_amount  REAL DEFAULT 200,
            variance      REAL DEFAULT 0,
            cash_sales    REAL DEFAULT 0,
            mpesa_sales   REAL DEFAULT 0,
            card_sales    REAL DEFAULT 0,
            split_sales   REAL DEFAULT 0,
            total_sales   REAL DEFAULT 0,
            total_orders  INTEGER DEFAULT 0,
            total_refunds REAL DEFAULT 0,
            notes         TEXT DEFAULT '',
            status        TEXT DEFAULT 'open',
            closed_at     TEXT,
            created_at    TEXT DEFAULT (datetime('now'))
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS credit_sales (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            sale_number   TEXT UNIQUE NOT NULL,
            customer_id   INTEGER NOT NULL,
            customer_name TEXT NOT NULL,
            subtotal      REAL DEFAULT 0,
            tax           REAL DEFAULT 0,
            discount      REAL DEFAULT 0,
            total_amount  REAL NOT NULL,
            amount_paid   REAL DEFAULT 0,
            balance       REAL NOT NULL,
            due_date      TEXT,
            status        TEXT DEFAULT 'unpaid',
            notes         TEXT DEFAULT '',
            cashier_id    INTEGER,
            created_at    TEXT DEFAULT (datetime('now')),
            updated_at    TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (customer_id) REFERENCES customers(id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS credit_payments (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            credit_sale_id  INTEGER NOT NULL,
            amount          REAL NOT NULL,
            payment_method  TEXT DEFAULT 'cash',
            notes           TEXT DEFAULT '',
            recorded_by     INTEGER,
            created_at      TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (credit_sale_id) REFERENCES credit_sales(id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS credit_sale_items (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            credit_sale_id INTEGER NOT NULL,
            product_id     INTEGER,
            product_name   TEXT NOT NULL,
            quantity       INTEGER NOT NULL,
            unit_price     REAL NOT NULL,
            total_price    REAL NOT NULL,
            FOREIGN KEY (credit_sale_id) REFERENCES credit_sales(id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS expiry_batches (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id     INTEGER NOT NULL,
            product_name   TEXT NOT NULL,
            batch_number   TEXT DEFAULT '',
            quantity       INTEGER DEFAULT 0,
            expiry_date    TEXT NOT NULL,
            cost_price     REAL DEFAULT 0,
            notes          TEXT DEFAULT '',
            created_at     TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (product_id) REFERENCES products(id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS budget_targets (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            category      TEXT NOT NULL,
            target_amount REAL NOT NULL,
            period        TEXT NOT NULL,
            created_at    TEXT DEFAULT (datetime('now'))
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS staff_pins (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER UNIQUE NOT NULL,
            pin_hash   TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS revoked_tokens (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            token      TEXT UNIQUE NOT NULL,
            revoked_at TEXT DEFAULT (datetime('now'))
        )
    """)

    # Clean up tokens older than 24h (max JWT lifetime) to keep table small
    cursor.execute(
        "DELETE FROM revoked_tokens WHERE revoked_at < datetime('now', '-24 hours')"
    )

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS alert_config (
            id                      INTEGER PRIMARY KEY DEFAULT 1,
            email_recipient         TEXT DEFAULT '',
            daily_summary_enabled   INTEGER DEFAULT 1,
            daily_summary_time      TEXT DEFAULT '20:00',
            reorder_alerts_enabled  INTEGER DEFAULT 1,
            low_stock_threshold_pct INTEGER DEFAULT 20,
            overdue_credit_alerts   INTEGER DEFAULT 1,
            last_summary_sent       TEXT DEFAULT '',
            updated_at              TEXT DEFAULT (datetime('now'))
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS alert_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            type       TEXT NOT NULL,
            title      TEXT NOT NULL,
            message    TEXT DEFAULT '',
            severity   TEXT DEFAULT 'info',
            is_read    INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS ai_conversations (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER,
            session_id TEXT DEFAULT '',
            role       TEXT NOT NULL,
            message    TEXT NOT NULL,
            context    TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)

    # Default alert config row
    cursor.execute("INSERT OR IGNORE INTO alert_config (id) VALUES (1)")

    # ── Payroll tables ────────────────────────────────────────────────────────
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS payroll_configs (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id               INTEGER UNIQUE NOT NULL,
            pay_type              TEXT DEFAULT 'monthly',
            basic_salary          REAL DEFAULT 0,
            house_allowance       REAL DEFAULT 0,
            transport_allowance   REAL DEFAULT 0,
            airtime_allowance     REAL DEFAULT 0,
            other_allowances      REAL DEFAULT 0,
            other_allowance_label TEXT DEFAULT '',
            loan_deduction        REAL DEFAULT 0,
            sacco_deduction       REAL DEFAULT 0,
            advance_deduction     REAL DEFAULT 0,
            other_deductions      REAL DEFAULT 0,
            other_deduction_label TEXT DEFAULT '',
            nssf_exempt           INTEGER DEFAULT 0,
            nhif_exempt           INTEGER DEFAULT 0,
            housing_levy_exempt   INTEGER DEFAULT 0,
            notes                 TEXT DEFAULT '',
            created_at            TEXT DEFAULT (datetime('now')),
            updated_at            TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS payroll_runs (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            period       TEXT NOT NULL,
            status       TEXT DEFAULT 'draft',
            staff_count  INTEGER DEFAULT 0,
            total_gross  REAL DEFAULT 0,
            total_net    REAL DEFAULT 0,
            total_paye   REAL DEFAULT 0,
            total_nssf   REAL DEFAULT 0,
            total_nhif   REAL DEFAULT 0,
            notes        TEXT DEFAULT '',
            payment_date TEXT,
            approved_by  INTEGER,
            created_by   INTEGER,
            created_at   TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (created_by)  REFERENCES users(id),
            FOREIGN KEY (approved_by) REFERENCES users(id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS payroll_payslips (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id           INTEGER NOT NULL,
            user_id          INTEGER NOT NULL,
            period           TEXT NOT NULL,
            full_name        TEXT NOT NULL,
            gross_pay        REAL DEFAULT 0,
            net_pay          REAL DEFAULT 0,
            paye             REAL DEFAULT 0,
            nssf_employee    REAL DEFAULT 0,
            nhif             REAL DEFAULT 0,
            housing_levy     REAL DEFAULT 0,
            total_statutory  REAL DEFAULT 0,
            total_voluntary  REAL DEFAULT 0,
            payslip_data     TEXT DEFAULT '{}',
            status           TEXT DEFAULT 'draft',
            payment_date     TEXT,
            created_at       TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (run_id)  REFERENCES payroll_runs(id),
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_payroll_runs_period   ON payroll_runs(period)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_payslips_run          ON payroll_payslips(run_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_payslips_user         ON payroll_payslips(user_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_payroll_configs_user  ON payroll_configs(user_id)")

    # ── Indexes (critical for query performance) ──────────────────────────────
    indexes = [
        ("idx_orders_created_at",       "orders(created_at)"),
        ("idx_orders_cashier_id",        "orders(cashier_id)"),
        ("idx_orders_status",            "orders(status)"),
        ("idx_orders_payment_method",    "orders(payment_method)"),
        ("idx_products_sku",             "products(sku)"),
        ("idx_products_barcode",         "products(barcode)"),
        ("idx_products_category",        "products(category)"),
        ("idx_products_supplier",        "products(supplier_id)"),
        ("idx_customers_phone",          "customers(phone)"),
        ("idx_customers_is_active",      "customers(is_active)"),
        ("idx_order_items_order_id",     "order_items(order_id)"),
        ("idx_order_items_product_id",   "order_items(product_id)"),
        ("idx_invoices_status",          "invoices(status)"),
        ("idx_expenses_date",            "expenses(date)"),
        ("idx_expenses_category",        "expenses(category)"),
        ("idx_shifts_user_id",           "staff_shifts(user_id)"),
        ("idx_access_log_created",       "access_log(created_at)"),
        ("idx_access_log_user",          "access_log(user_id)"),
        ("idx_access_log_action",        "access_log(action)"),
        ("idx_credit_sales_status",      "credit_sales(status)"),
        ("idx_credit_sales_customer",    "credit_sales(customer_id)"),
        ("idx_credit_sales_due",         "credit_sales(due_date)"),
        ("idx_mpesa_checkout",           "mpesa_transactions(checkout_request_id)"),
        ("idx_product_variants_product", "product_variants(product_id)"),
        ("idx_product_variants_barcode", "product_variants(barcode)"),
        ("idx_promotions_active",        "promotions(is_active)"),
        ("idx_expiry_date",              "expiry_batches(expiry_date)"),
        ("idx_expiry_product",           "expiry_batches(product_id)"),
        ("idx_ai_conv_user",             "ai_conversations(user_id, created_at)"),
        ("idx_cash_ups_date",            "cash_ups(shift_date)"),
        ("idx_po_items_po",              "po_items(po_id)"),
        ("idx_revoked_tokens",            "revoked_tokens(token)"),
        ("idx_revoked_tokens_time",       "revoked_tokens(revoked_at)"),
    ]
    for name, cols in indexes:
        cursor.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {cols}")

    # ── Default settings ──────────────────────────────────────────────────────
    defaults = [
        ("business_name",    "Neural POS"),
        ("business_phone",   "+254700000000"),
        ("business_address", "Nairobi, Kenya"),
        ("currency",         "KES"),
        ("vat_rate",         "16"),
        ("receipt_footer",   "Thank you for your business!"),
        ("opening_float",    "200"),
    ]
    for k, v in defaults:
        cursor.execute("INSERT OR IGNORE INTO settings (key,value) VALUES (?,?)", (k, v))

    # ── Seed data ─────────────────────────────────────────────────────────────
    cursor.execute("""
        INSERT OR IGNORE INTO suppliers
        (id, name, contact_person, email, phone, city, country, payment_terms, lead_time_days)
        VALUES
        (1,'TechDistributors Ltd','Alice Mwangi','alice@techdist.co.ke','+254711000001','Nairobi','Kenya','Net 30',7),
        (2,'OfficeSupplies Co','Bob Kamau','bob@officesupplies.co.ke','+254711000002','Nairobi','Kenya','Net 15',3),
        (3,'SmartGadgets Kenya','Carol Njeri','carol@smartgadgets.co.ke','+254711000003','Mombasa','Kenya','COD',14)
    """)

    cursor.execute("""
        INSERT OR IGNORE INTO products
        (sku,name,description,category,price,cost,stock_quantity,reorder_level,supplier_id)
        VALUES
        ('SKU-001','Wireless Headphones Pro','Premium noise-cancelling headphones','Electronics',14999,7500,45,10,1),
        ('SKU-002','Mechanical Keyboard','RGB backlit mechanical keyboard','Electronics',8999,4000,32,8,1),
        ('SKU-003','USB-C Hub 7-in-1','Multi-port USB-C hub','Accessories',4999,2000,78,15,3),
        ('SKU-004','Laptop Stand Aluminium','Adjustable aluminium laptop stand','Accessories',3999,1500,55,12,2),
        ('SKU-005','Webcam 4K Ultra','4K resolution webcam with autofocus','Electronics',11999,5500,28,8,1),
        ('SKU-006','Mouse Pad XL','Extra-large gaming mouse pad','Accessories',2499,800,120,20,3),
        ('SKU-007','Cable Management Kit','Desk cable management organizer','Accessories',1999,600,200,30,2),
        ('SKU-008','Monitor Light Bar','USB-powered monitor light bar','Accessories',3499,1400,62,10,1),
        ('SKU-009','Portable SSD 1TB','High-speed portable solid state drive','Storage',9999,5000,40,10,3),
        ('SKU-010','Smart LED Strip 5m','App-controlled LED strip lighting','Smart Home',2999,1000,85,15,2)
    """)

    cursor.execute("""
        INSERT OR IGNORE INTO users (username, password_hash, full_name, role, email)
        VALUES
        ('admin',    ?, 'System Administrator', 'admin',   'admin@neuralpos.com'),
        ('manager',  ?, 'Store Manager',         'manager', 'manager@neuralpos.com'),
        ('cashier1', ?, 'Jane Cashier',           'cashier', 'cashier@neuralpos.com')
    """, (hash_password("admin123"), hash_password("manager123"), hash_password("cashier123")))

    cursor.execute("""
        INSERT OR IGNORE INTO customers
        (id,name,phone,email,loyalty_points,credit_limit,total_spent,total_orders)
        VALUES
        (2,'John Kamau','+254712345678','john.kamau@gmail.com',250,5000,12500,48),
        (3,'Mary Wanjiku','+254798765432','mary.w@gmail.com',180,3000,8900,35)
    """)

    conn.commit()
    conn.close()
