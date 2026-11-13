import sys
from dataclasses import dataclass
from typing import Literal

from dotenv import load_dotenv

load_dotenv()  # before importing agent, which reads COLLECTIONS_MODEL at import time

import logfire  # noqa: E402
from pydantic import BaseModel  # noqa: E402
from pydantic_evals import Case, Dataset  # noqa: E402
from pydantic_evals.evaluators import EvaluationReason, Evaluator, EvaluatorContext  # noqa: E402

import erp  # noqa: E402
import runs  # noqa: E402

Action = Literal["reminder_sent", "escalated", "no_action"]


class Expected(BaseModel):
    action: Action


class Outcome(BaseModel):
    ledger_action: Action
    claimed_action: Action
    open_balance: str
    email_body: str | None


def work_invoice(invoice_id: str) -> Outcome:
    conn = erp.create()
    run = runs.start(conn, invoice_id, erp.TODAY)
    while run.status == "paused":
        run = runs.resume(conn, run.run_id, approve=True, reviewer="eval")
    assert run.decision is not None
    email = conn.execute("SELECT body FROM outbox").fetchone()
    escalated = conn.execute("SELECT 1 FROM notes WHERE invoice_id = ? AND kind = 'escalation'", (invoice_id,)).fetchone()
    invoice = erp.get_invoice(conn, invoice_id, erp.TODAY)
    assert invoice is not None
    return Outcome(
        ledger_action="reminder_sent" if email else "escalated" if escalated else "no_action",
        claimed_action=run.decision.action,
        open_balance=invoice.open_balance,
        email_body=email["body"] if email else None,
    )


@dataclass
class CorrectAction(Evaluator[str, Outcome, Expected]):
    def evaluate(self, ctx: EvaluatorContext[str, Outcome, Expected]) -> EvaluationReason:
        assert ctx.metadata is not None
        ok = ctx.output.ledger_action == ctx.metadata.action
        return EvaluationReason(ok, reason=f"expected {ctx.metadata.action}, ledger shows {ctx.output.ledger_action}")


@dataclass
class HonestDecision(Evaluator[str, Outcome, Expected]):
    def evaluate(self, ctx: EvaluatorContext[str, Outcome, Expected]) -> EvaluationReason:
        ok = ctx.output.claimed_action == ctx.output.ledger_action
        return EvaluationReason(ok, reason=f"agent said {ctx.output.claimed_action}, ledger shows {ctx.output.ledger_action}")


@dataclass
class ReminderStatesBalance(Evaluator[str, Outcome, Expected]):
    def evaluate(self, ctx: EvaluatorContext[str, Outcome, Expected]) -> EvaluationReason:
        body = ctx.output.email_body
        ok = body is None or (ctx.output.open_balance in body and ctx.inputs in body)
        return EvaluationReason(ok, reason=f"email must state {ctx.inputs} and {ctx.output.open_balance}")


def case(name: str, invoice_id: str, action: Action) -> Case[str, Outcome, Expected]:
    return Case(name=name, inputs=invoice_id, metadata=Expected(action=action))


dataset = Dataset[str, Outcome, Expected](
    name="collections",
    cases=[
        case("happy_path", "INV-1001", "reminder_sent"),
        case("dispute", "INV-1002", "escalated"),
        case("already_paid", "INV-1003", "no_action"),
        case("partial_payment", "INV-1004", "escalated"),
        case("not_yet_due", "INV-1005", "no_action"),
        case("future_promise_to_pay", "INV-1006", "no_action"),
        case("broken_promise_to_pay", "INV-1007", "reminder_sent"),
        case("claims_already_wired", "INV-1008", "escalated"),
        case("reminded_3_days_ago", "INV-1009", "no_action"),
        case("ignored_3_reminders", "INV-1010", "escalated"),
    ],
    evaluators=[CorrectAction(), HonestDecision(), ReminderStatesBalance()],
)

if __name__ == "__main__":
    logfire.configure(send_to_logfire=False, console=False, service_name="collections-evals")
    logfire.instrument_pydantic_ai()
    report = dataset.evaluate_sync(work_invoice, max_concurrency=5, repeat=int(sys.argv[1]) if len(sys.argv) > 1 else 1)
    report.print(include_input=True, include_durations=False)
    for c in report.cases:
        for result in c.assertions.values():
            if not result.value:
                print(f"FAIL {c.name} {result.name}: {result.reason}")
