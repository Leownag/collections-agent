import sqlite3
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

TODAY = date(2026, 11, 12)

SCHEMA = """
CREATE TABLE customers (id TEXT PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL);
CREATE TABLE invoices (
    id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL REFERENCES customers(id),
    amount_cents INTEGER NOT NULL,
    due_date TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('open', 'paid'))
);
CREATE TABLE payments (
    id INTEGER PRIMARY KEY,
    invoice_id TEXT NOT NULL REFERENCES invoices(id),
    amount_cents INTEGER NOT NULL,
    received_on TEXT NOT NULL
);
CREATE TABLE promises (
    id INTEGER PRIMARY KEY,
    invoice_id TEXT NOT NULL REFERENCES invoices(id),
    promised_on TEXT NOT NULL,
    recorded_on TEXT NOT NULL
);
CREATE TABLE notes (
    id INTEGER PRIMARY KEY,
    invoice_id TEXT NOT NULL REFERENCES invoices(id),
    created_on TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('customer_email', 'reminder', 'escalation')),
    body TEXT NOT NULL
);

CREATE TABLE outbox (
    id INTEGER PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    to_email TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL
);
CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY,
    idempotency_key TEXT UNIQUE,
    run_id TEXT NOT NULL,
    trace_id TEXT,
    action TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TABLE runs (run_id TEXT PRIMARY KEY, state TEXT NOT NULL, messages TEXT NOT NULL);
"""

CUSTOMERS = [
    ("C01", "Acme Corp", "ap@acme.example"),
    ("C02", "Birch Foods", "payables@birch.example"),
    ("C03", "Cobalt Labs", "finance@cobalt.example"),
    ("C04", "Dune Logistics", "ap@dune.example"),
    ("C05", "Elm Studio", "billing@elm.example"),
    ("C06", "Fjord Retail", "ap@fjord.example"),
    ("C07", "Grove Health", "accounts@grove.example"),
    ("C08", "Harbor Steel", "treasury@harbor.example"),
    ("C09", "Iris Media", "ap@iris.example"),
    ("C10", "Juniper Dental", "office@juniper.example"),
]
INVOICES = [
    ("INV-1001", "C01", 1_250_000, "2026-10-20", "open"),
    ("INV-1002", "C02", 840_000, "2026-10-15", "open"),
    ("INV-1003", "C03", 320_000, "2026-10-01", "paid"),
    ("INV-1004", "C04", 1_000_000, "2026-10-10", "open"),
    ("INV-1005", "C05", 275_000, "2026-11-30", "open"),
    ("INV-1006", "C06", 560_000, "2026-10-25", "open"),
    ("INV-1007", "C07", 410_000, "2026-09-30", "open"),
    ("INV-1008", "C08", 1_500_000, "2026-10-05", "open"),
    ("INV-1009", "C09", 190_000, "2026-10-20", "open"),
    ("INV-1010", "C10", 630_000, "2026-07-15", "open"),
]
PAYMENTS = [
    ("INV-1003", 320_000, "2026-10-05"),
    ("INV-1004", 600_000, "2026-10-30"),
]
PROMISES = [
    ("INV-1006", "2026-11-20", "2026-11-08"),
    ("INV-1007", "2026-10-31", "2026-10-10"),
]
NOTES = [
    ("INV-1002", "2026-10-28", "customer_email",
     "Half the pallets on this shipment arrived damaged. We won't pay this invoice until you issue a credit."),
    ("INV-1006", "2026-11-08", "customer_email", "Our next AP run is Nov 20. You'll have the payment then."),
    ("INV-1007", "2026-10-10", "customer_email", "Sorry for the delay, we will pay by Oct 31."),
    ("INV-1008", "2026-11-10", "customer_email", "We wired the full amount last Thursday, reference HS-88812."),
    ("INV-1009", "2026-11-09", "reminder", "Payment reminder sent to ap@iris.example."),
    ("INV-1010", "2026-08-15", "reminder", "Payment reminder sent to office@juniper.example."),
    ("INV-1010", "2026-09-15", "reminder", "Payment reminder sent to office@juniper.example."),
    ("INV-1010", "2026-10-15", "reminder", "Payment reminder sent to office@juniper.example."),
]


class Invoice(BaseModel):
    id: str
    customer_name: str
    customer_email: str
    status: Literal["open", "paid"]
    due_date: date
    days_past_due: int
    amount_cents: int
    open_balance_cents: int
    amount: str
    open_balance: str
    promised_to_pay_on: date | None
    promise_status: Literal["none", "pending", "broken"]


class Payment(BaseModel):
    amount: str
    received_on: date


class Note(BaseModel):
    created_on: date
    kind: Literal["customer_email", "reminder", "escalation"]
    body: str


def usd(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def connect(path: str) -> sqlite3.Connection:
    # Pydantic AI runs sync tools in worker threads; SQLite is built serialized (threadsafety=3), so sharing is safe.
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def create(path: str = ":memory:") -> sqlite3.Connection:
    if path != ":memory:":
        Path(path).unlink(missing_ok=True)
    conn = connect(path)
    conn.executescript(SCHEMA)
    with conn:
        conn.executemany("INSERT INTO customers VALUES (?, ?, ?)", CUSTOMERS)
        conn.executemany("INSERT INTO invoices VALUES (?, ?, ?, ?, ?)", INVOICES)
        conn.executemany("INSERT INTO payments (invoice_id, amount_cents, received_on) VALUES (?, ?, ?)", PAYMENTS)
        conn.executemany("INSERT INTO promises (invoice_id, promised_on, recorded_on) VALUES (?, ?, ?)", PROMISES)
        conn.executemany("INSERT INTO notes (invoice_id, created_on, kind, body) VALUES (?, ?, ?, ?)", NOTES)
    return conn


def get_invoice(conn: sqlite3.Connection, invoice_id: str, today: date) -> Invoice | None:
    row = conn.execute(
        """
        SELECT i.id, c.name, c.email, i.status, i.due_date, i.amount_cents,
               i.amount_cents - COALESCE((SELECT SUM(amount_cents) FROM payments WHERE invoice_id = i.id), 0) AS open_cents,
               (SELECT promised_on FROM promises WHERE invoice_id = i.id ORDER BY recorded_on DESC LIMIT 1) AS promised_on
        FROM invoices i JOIN customers c ON c.id = i.customer_id
        WHERE i.id = ?
        """,
        (invoice_id,),
    ).fetchone()
    if row is None:
        return None
    due = date.fromisoformat(row["due_date"])
    promised = date.fromisoformat(row["promised_on"]) if row["promised_on"] else None
    return Invoice(
        id=row["id"],
        customer_name=row["name"],
        customer_email=row["email"],
        status=row["status"],
        due_date=due,
        days_past_due=(today - due).days,
        amount_cents=row["amount_cents"],
        open_balance_cents=row["open_cents"],
        amount=usd(row["amount_cents"]),
        open_balance=usd(row["open_cents"]),
        promised_to_pay_on=promised,
        promise_status="none" if promised is None else "pending" if promised >= today else "broken",
    )


def list_payments(conn: sqlite3.Connection, invoice_id: str) -> list[Payment]:
    rows = conn.execute(
        "SELECT amount_cents, received_on FROM payments WHERE invoice_id = ? ORDER BY received_on", (invoice_id,)
    )
    return [Payment(amount=usd(r["amount_cents"]), received_on=r["received_on"]) for r in rows]


def list_notes(conn: sqlite3.Connection, invoice_id: str) -> list[Note]:
    rows = conn.execute(
        "SELECT created_on, kind, body FROM notes WHERE invoice_id = ? ORDER BY created_on, id", (invoice_id,)
    )
    return [Note(created_on=r["created_on"], kind=r["kind"], body=r["body"]) for r in rows]
