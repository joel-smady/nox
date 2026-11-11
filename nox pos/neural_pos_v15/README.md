# ⚡ Neural POS

An intelligent Point of Sale system powered by Llama LLM — built with FastAPI, dark neural aesthetics, and role-based access control.

---

## Features

- **Role-based auth** — Admin, Manager, Cashier with JWT tokens
- **POS Terminal** — Cart, barcode search, M-Pesa / Cash / Card checkout
- **Inventory Management** — Full CRUD, stock adjustments, low-stock alerts
- **Supplier Management** — Supplier directory + purchase orders
- **Analytics** — Revenue trends, top products, cashier performance, hourly sales
- **Admin Panel** — User management, audit log, system stats
- **AI Assistant** — Llama-powered business intelligence (via Ollama)

---

## Quick Start

### 1. Install Python dependencies

```bash
cd neural_pos
pip install -r requirements.txt
```

### 2. (Optional) Set up Llama AI

Install [Ollama](https://ollama.com) then run:

```bash
ollama serve
ollama pull llama3.1
```

> The AI assistant works with graceful fallback responses even without Ollama.

### 3. Run the server

```bash
python main.py
```

Visit: **http://localhost:8000**

---

## Default Credentials

| Role     | Username  | Password     |
|----------|-----------|--------------|
| Admin    | admin     | admin123     |
| Manager  | manager   | manager123   |
| Cashier  | cashier1  | cashier123   |

---

## Project Structure

```
neural_pos/
├── main.py                  # FastAPI app entry point
├── requirements.txt
├── models/
│   └── database.py          # SQLite schema, seed data
├── routers/
│   ├── auth.py              # JWT authentication
│   ├── pos.py               # Sales & orders
│   ├── inventory.py         # Products & stock
│   ├── suppliers.py         # Suppliers & purchase orders
│   ├── analytics.py         # Business analytics
│   ├── admin.py             # User & system admin
│   └── ai_assistant.py      # Llama AI chat
├── templates/
│   ├── login.html           # Login page
│   └── dashboard.html       # Full SPA dashboard
└── data/
    └── neural_pos.db        # Auto-created SQLite DB
```

---

## API Reference

| Prefix            | Description              |
|-------------------|--------------------------|
| `/api/auth`       | Login, logout, verify    |
| `/api/pos`        | Sales, cart, orders      |
| `/api/inventory`  | Products, stock          |
| `/api/suppliers`  | Suppliers, POs           |
| `/api/analytics`  | Reports, trends          |
| `/api/admin`      | Users, audit log         |
| `/api/ai`         | Llama chat, history      |

Interactive docs: **http://localhost:8000/docs**

---

## Design

- **Color palette**: `#0a0a0f` void black · `#7c3aed` neural purple · `#e879f9` accent magenta · `#f5f0e8` cream
- **Fonts**: Space Grotesk · JetBrains Mono · Bebas Neue
- **Charts**: Chart.js 4.4.0
- **Database**: SQLite (zero-config, auto-initialized)
- **VAT**: 16% (Kenya market)

---

## License

MIT
