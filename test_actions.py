import sqlite3

import pytest

import actions
import erp


def send(conn: sqlite3.Connection) -> str:
    return actions.send_reminder(conn, run_id="r1", invoice_id="INV-1001", subject="s", body="b", today=erp.TODAY)


def test_retried_reminder_is_sent_once() -> None:
    conn = erp.create()
    assert send(conn).startswith("Reminder queued")
    assert send(conn).startswith("Already sent")
    assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'send_reminder'").fetchone()[0] == 1


def test_failed_write_leaves_no_audit_row() -> None:
    conn = erp.create()
    conn.execute("DROP TABLE outbox")
    with pytest.raises(sqlite3.OperationalError):
        send(conn)
    assert conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 0


def test_audit_log_is_append_only() -> None:
    conn = erp.create()
    actions.record(conn, run_id="r1", action="run_started", payload={})
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM audit_log")


def test_reminder_blockers() -> None:
    conn = erp.create()
    assert actions.reminder_blockers(conn, "INV-1001", erp.TODAY) == []
    assert "invoice is not open" in actions.reminder_blockers(conn, "INV-1003", erp.TODAY)
    assert "invoice is not past due" in actions.reminder_blockers(conn, "INV-1005", erp.TODAY)
    assert any("partially paid" in b for b in actions.reminder_blockers(conn, "INV-1004", erp.TODAY))
    assert actions.reminder_blockers(conn, "INV-1006", erp.TODAY) == ["customer promised to pay by 2026-11-20"]
    assert actions.reminder_blockers(conn, "INV-1007", erp.TODAY) == []
