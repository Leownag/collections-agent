import sqlite3
import uuid
from datetime import date
from typing import Any, Literal

from opentelemetry import trace
from pydantic import BaseModel
from pydantic_ai import AgentRunResult, DeferredToolRequests, DeferredToolResults, ModelMessagesTypeAdapter, ToolDenied

import actions
from agent import Decision, Deps, agent

tracer = trace.get_tracer("collections-agent")


class PendingApproval(BaseModel):
    tool_call_id: str
    tool_name: str
    args: dict[str, Any]


class Run(BaseModel):
    run_id: str
    invoice_id: str
    today: date
    status: Literal["paused", "done"]
    pending: list[PendingApproval] = []
    decision: Decision | None = None


def start(conn: sqlite3.Connection, invoice_id: str, today: date) -> Run:
    run_id = uuid.uuid4().hex[:8]
    with tracer.start_as_current_span("collections run", attributes={"run_id": run_id, "invoice_id": invoice_id}):
        actions.record(conn, run_id=run_id, action="run_started", payload={"invoice_id": invoice_id})
        result = agent.run_sync(f"Work invoice {invoice_id}.", deps=Deps(conn, run_id, invoice_id, today))
        return _save(conn, run_id, invoice_id, today, result)


def resume(conn: sqlite3.Connection, run_id: str, *, approve: bool, reviewer: str, note: str = "") -> Run:
    row = conn.execute("SELECT state, messages FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    run = Run.model_validate_json(row["state"])
    if run.status != "paused":
        raise ValueError(f"run {run_id} is {run.status}, not paused")
    with tracer.start_as_current_span("collections resume", attributes={"run_id": run_id, "approved": approve}):
        actions.record(
            conn,
            run_id=run_id,
            action="approved" if approve else "rejected",
            payload={"reviewer": reviewer, "note": note, "tool_calls": [p.model_dump() for p in run.pending]},
        )
        results = DeferredToolResults()
        for p in run.pending:
            results.approvals[p.tool_call_id] = True if approve else ToolDenied(f"Rejected by {reviewer}: {note}")
        result = agent.run_sync(
            message_history=ModelMessagesTypeAdapter.validate_json(row["messages"]),
            deferred_tool_results=results,
            deps=Deps(conn, run_id, run.invoice_id, run.today),
        )
        return _save(conn, run_id, run.invoice_id, run.today, result)


def _save(
    conn: sqlite3.Connection,
    run_id: str,
    invoice_id: str,
    today: date,
    result: AgentRunResult[Decision | DeferredToolRequests],
) -> Run:
    if isinstance(result.output, DeferredToolRequests):
        pending = [PendingApproval(tool_call_id=c.tool_call_id, tool_name=c.tool_name, args=c.args_as_dict()) for c in result.output.approvals]
        run = Run(run_id=run_id, invoice_id=invoice_id, today=today, status="paused", pending=pending)
    else:
        run = Run(run_id=run_id, invoice_id=invoice_id, today=today, status="done", decision=result.output)
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO runs (run_id, state, messages) VALUES (?, ?, ?)",
            (run_id, run.model_dump_json(), result.all_messages_json()),
        )
        actions.append_audit(conn, None, run_id, "run_" + run.status, run.model_dump(mode="json"))
    return run
