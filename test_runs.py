from pathlib import Path

from pydantic_ai import ModelMessage, ModelResponse, RetryPromptPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

import erp
import runs
from agent import agent

REMINDER = {"invoice_id": "INV-1001", "subject": "Invoice INV-1001", "body": "Please pay $12,500.00.", "confidence": 0.9}


def scripted_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    answers = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart | RetryPromptPart)]
    if not answers:
        return ModelResponse(parts=[ToolCallPart("send_reminder", REMINDER)])
    action = "reminder_sent" if isinstance(answers[-1], ToolReturnPart) else "no_action"
    return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"action": action, "reason": str(answers[-1].content)})])


def test_run_pauses_for_approval_then_resumes_and_survives_a_crash(tmp_path: Path) -> None:
    db = str(tmp_path / "erp.db")
    conn = erp.create(db)
    with agent.override(model=FunctionModel(scripted_model)):
        paused = runs.start(conn, "INV-1001", erp.TODAY)
        assert paused.status == "paused" and paused.pending[0].tool_name == "send_reminder"
        assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0

        snapshot = conn.execute("SELECT state, messages FROM runs").fetchone()
        conn = erp.connect(db)
        done = runs.resume(conn, paused.run_id, approve=True, reviewer="leo")
        assert done.decision is not None and done.decision.action == "reminder_sent"

        conn.execute("UPDATE runs SET state = ?, messages = ?", (snapshot["state"], snapshot["messages"]))
        retried = runs.resume(conn, paused.run_id, approve=True, reviewer="leo")
        assert retried.decision is not None and "Already sent" in retried.decision.reason
    assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 1


def test_payment_during_pause_blocks_the_approved_reminder() -> None:
    conn = erp.create()
    with agent.override(model=FunctionModel(scripted_model)):
        paused = runs.start(conn, "INV-1001", erp.TODAY)
        with conn:
            conn.execute("INSERT INTO payments (invoice_id, amount_cents, received_on) VALUES ('INV-1001', 1250000, '2026-11-12')")
        done = runs.resume(conn, paused.run_id, approve=True, reviewer="leo")
    assert done.decision is not None and "nothing is owed" in done.decision.reason
    assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0


def test_rejected_reminder_is_not_sent() -> None:
    conn = erp.create()
    with agent.override(model=FunctionModel(scripted_model)):
        paused = runs.start(conn, "INV-1001", erp.TODAY)
        runs.resume(conn, paused.run_id, approve=False, reviewer="leo", note="customer called today")
    assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0
