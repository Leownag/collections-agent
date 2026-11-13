import json
import sqlite3
from datetime import UTC, date, datetime
from typing import Any, Literal

from opentelemetry import trace

import erp

EscalationCategory = Literal["dispute", "partial_payment", "claimed_payment", "unresponsive", "other"]


def reminder_blockers(conn: sqlite3.Connection, invoice_id: str, today: date) -> list[str]:
    invoice = erp.get_invoice(conn, invoice_id, today)
    if invoice is None:
        return [f"invoice {invoice_id} does not exist"]
    blockers = []
    if invoice.status != "open":
        blockers.append("invoice is not open")
    if invoice.open_balance_cents <= 0:
        blockers.append("nothing is owed")
    elif invoice.open_balance_cents < invoice.amount_cents:
        blockers.append("invoice is partially paid, and short payments go to a human (possible deduction)")
    if invoice.days_past_due <= 0:
        blockers.append("invoice is not past due")
    if invoice.promise_status == "pending":
        blockers.append(f"customer promised to pay by {invoice.promised_to_pay_on}")
    return blockers


def send_reminder(
    conn: sqlite3.Connection, *, run_id: str, invoice_id: str, subject: str, body: str, today: date
) -> str:
    invoice = erp.get_invoice(conn, invoice_id, today)
    assert invoice is not None
    key = f"reminder:{invoice_id}:{today.isoformat()}"
    with conn:
        if not _claim(conn, key, run_id, "send_reminder", {"invoice_id": invoice_id, "subject": subject, "body": body}):
            return f"Already sent today (idempotency key {key}). Nothing new was sent."
        conn.execute(
            "INSERT INTO outbox (idempotency_key, to_email, subject, body) VALUES (?, ?, ?, ?)",
            (key, invoice.customer_email, subject, body),
        )
        conn.execute(
            "INSERT INTO notes (invoice_id, created_on, kind, body) VALUES (?, ?, 'reminder', ?)",
            (invoice_id, today.isoformat(), f"Payment reminder sent to {invoice.customer_email}."),
        )
    return f"Reminder queued to {invoice.customer_email}."


def escalate(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    invoice_id: str,
    category: EscalationCategory,
    summary: str,
    today: date,
) -> str:
    key = f"escalate:{invoice_id}:{category}"
    with conn:
        if not _claim(conn, key, run_id, "escalate", {"invoice_id": invoice_id, "category": category, "summary": summary}):
            return f"Already escalated as {category}."
        conn.execute(
            "INSERT INTO notes (invoice_id, created_on, kind, body) VALUES (?, ?, 'escalation', ?)",
            (invoice_id, today.isoformat(), f"[{category}] {summary}"),
        )
    return f"Escalated to a human collector as {category}."


def record(conn: sqlite3.Connection, *, run_id: str, action: str, payload: dict[str, Any]) -> None:
    with conn:
        append_audit(conn, None, run_id, action, payload)


def _claim(conn: sqlite3.Connection, key: str, run_id: str, action: str, payload: dict[str, Any]) -> bool:
    try:
        append_audit(conn, key, run_id, action, payload)
    except sqlite3.IntegrityError:
        return False
    return True


def append_audit(
    conn: sqlite3.Connection, key: str | None, run_id: str, action: str, payload: dict[str, Any]
) -> None:
    span = trace.get_current_span().get_span_context()
    conn.execute(
        "INSERT INTO audit_log (idempotency_key, run_id, trace_id, action, payload, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (
            key,
            run_id,
            format(span.trace_id, "032x") if span.is_valid else None,
            action,
            json.dumps(payload),
            datetime.now(UTC).isoformat(),
        ),
    )
