import sys

from dotenv import load_dotenv

load_dotenv()  # before importing agent, which reads COLLECTIONS_MODEL at import time

import logfire  # noqa: E402

import erp  # noqa: E402
import runs  # noqa: E402

DB = "demo.db"


def main(invoice_id: str) -> None:
    logfire.configure(send_to_logfire=False, service_name="collections-agent")
    logfire.instrument_pydantic_ai()

    run = runs.start(erp.create(DB), invoice_id, erp.TODAY)
    print(f"\n== run {run.run_id} on {invoice_id}: {run.status}")
    if run.status == "paused":
        for p in run.pending:
            print(f"\n-- needs approval: {p.tool_name} (confidence {p.args['confidence']})")
            print(f"Subject: {p.args['subject']}\n\n{p.args['body']}\n")
        approve = input("Approve? [y/N] ").strip().lower() == "y" if sys.stdin.isatty() else True
        conn = erp.connect(DB)
        run = runs.resume(conn, run.run_id, approve=approve, reviewer="leo")
        print(f"\n== run {run.run_id} resumed after {'approval' if approve else 'rejection'}: {run.status}")

    assert run.decision is not None
    print(f"Decision: {run.decision.action}: {run.decision.reason}")
    conn = erp.connect(DB)
    print("\n-- outbox")
    for r in conn.execute("SELECT idempotency_key, to_email, subject FROM outbox"):
        print(f"  {r['idempotency_key']} -> {r['to_email']}: {r['subject']}")
    print("\n-- audit_log")
    for r in conn.execute("SELECT id, run_id, action, idempotency_key, trace_id FROM audit_log ORDER BY id"):
        print(f"  #{r['id']} run={r['run_id']} {r['action']:<14} key={r['idempotency_key']} trace={(r['trace_id'] or '-')[:12]}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "INV-1001")
