import os
import sqlite3
from dataclasses import dataclass
from datetime import date
from typing import Literal

from pydantic import BaseModel
from pydantic_ai import Agent, ApprovalRequired, DeferredToolRequests, ModelRetry, RunContext

import actions
import erp

CONFIDENCE_THRESHOLD = 0.8

INSTRUCTIONS = """\
You are an accounts-receivable collections agent. You handle exactly one invoice per run.
Gather the facts with the read tools first, then take at most one action:

- send_reminder: the invoice is past due and nothing below applies. A promise-to-pay date that
  has already passed is a broken promise: send a reminder that mentions the promise.
- escalate: the customer disputes the invoice, paid only part of it, says they already paid,
  or has ignored 3 or more reminders.
- No action: the invoice is paid, not yet due, the customer promised to pay on a date that has
  not passed yet, or a reminder went out in the last 7 days.

Reminder emails are short and polite, name the invoice, the open balance and the due date, and
are signed "Accounts Receivable". Finish with a Decision that says what you did and why.
"""


@dataclass
class Deps:
    conn: sqlite3.Connection
    run_id: str
    invoice_id: str
    today: date


class Decision(BaseModel):
    action: Literal["reminder_sent", "escalated", "no_action"]
    reason: str


agent = Agent[Deps, Decision | DeferredToolRequests](
    os.environ.get("COLLECTIONS_MODEL", "openai:gpt-5.4-mini"),
    deps_type=Deps,
    output_type=[Decision, DeferredToolRequests],
    instructions=INSTRUCTIONS,
    retries=2,
    defer_model_check=True,
)


@agent.instructions
def run_context(ctx: RunContext[Deps]) -> str:
    return f"Today is {ctx.deps.today.isoformat()}. Your invoice is {ctx.deps.invoice_id}."


@agent.tool
def get_invoice(ctx: RunContext[Deps], invoice_id: str) -> erp.Invoice:
    """Look up an invoice: customer, status, due date, days past due and open balance."""
    invoice = erp.get_invoice(ctx.deps.conn, invoice_id, ctx.deps.today)
    if invoice is None:
        raise ModelRetry(f"There is no invoice {invoice_id}.")
    return invoice


@agent.tool
def list_payments(ctx: RunContext[Deps], invoice_id: str) -> list[erp.Payment]:
    """Payments applied to the invoice."""
    return erp.list_payments(ctx.deps.conn, invoice_id)


@agent.tool
def list_notes(ctx: RunContext[Deps], invoice_id: str) -> list[erp.Note]:
    """Customer emails, past reminders and escalations on the invoice, oldest first."""
    return erp.list_notes(ctx.deps.conn, invoice_id)


@agent.tool(sequential=True)
def send_reminder(ctx: RunContext[Deps], invoice_id: str, subject: str, body: str, confidence: float) -> str:
    """Email the customer a payment reminder. A human reviews it before it is sent.

    Args:
        invoice_id: The invoice to collect on.
        subject: Email subject line.
        body: Plain-text email body.
        confidence: 0 to 1, how sure you are that a reminder is the right action.
    """
    _check_scope(ctx, invoice_id)
    if blockers := actions.reminder_blockers(ctx.deps.conn, invoice_id, ctx.deps.today):
        raise ModelRetry(f"Reminder blocked: {'; '.join(blockers)}. Escalate or take no action instead.")
    if confidence < CONFIDENCE_THRESHOLD:
        raise ModelRetry(f"Confidence {confidence} is below {CONFIDENCE_THRESHOLD}. Escalate instead.")
    if not ctx.tool_call_approved:
        raise ApprovalRequired()
    return actions.send_reminder(
        ctx.deps.conn, run_id=ctx.deps.run_id, invoice_id=invoice_id, subject=subject, body=body, today=ctx.deps.today
    )


@agent.tool(sequential=True)
def escalate(ctx: RunContext[Deps], invoice_id: str, category: actions.EscalationCategory, summary: str) -> str:
    """Hand the invoice to a human collector with a one-paragraph summary of what you found."""
    _check_scope(ctx, invoice_id)
    return actions.escalate(
        ctx.deps.conn, run_id=ctx.deps.run_id, invoice_id=invoice_id, category=category, summary=summary, today=ctx.deps.today
    )


def _check_scope(ctx: RunContext[Deps], invoice_id: str) -> None:
    if invoice_id != ctx.deps.invoice_id:
        raise ModelRetry(f"You may only act on {ctx.deps.invoice_id}.")
