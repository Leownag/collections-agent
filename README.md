# collections-agent

A small accounts-receivable collections agent built on Pydantic AI. It runs on a fake ERP in SQLite. For one overdue invoice, it reads the account through tools and then takes one of three paths:

- It sends a reminder email, but only after a human approves it.
- It escalates the invoice to a human collector.
- It does nothing.

**Probabilistic reasoning, deterministic writes.** The model proposes. Plain Python checks the ledger and commits.

## Setup

```bash
python3.13 -m venv .venv
.venv/bin/pip install "pydantic-ai-slim[openai,logfire]==2.68.0" "pydantic-evals==2.68.0" python-dotenv mypy pytest
cp .env.example .env
```

Open `.env` and set `OPENAI_API_KEY` to your OpenAI key. You can create one at https://platform.openai.com/api-keys.

- The key is read only from the environment. `demo.py` and `evals.py` load `.env` with python-dotenv.
- `.env` is in `.gitignore`. Never commit it.
- To try another model, set `COLLECTIONS_MODEL` to any Pydantic AI model string that supports tool calling, for example `anthropic:claude-sonnet-5-5`. You also need that provider's key and its extra, for example `pydantic-ai-slim[anthropic]`.

## Run

```bash
.venv/bin/python demo.py INV-1001
```

This runs one invoice end to end. The agent pauses for your approval: type `y` to approve, or anything else to reject. It then resumes on a fresh DB connection and prints the outbox and the audit log. Traces print to the console through Logfire with `send_to_logfire=False`, so no account is needed.

```bash
.venv/bin/python evals.py 3
```

This runs 10 cases, 3 times each, and prints a pass/fail table. Failures are listed at the bottom. A full run costs about $0.08 on `gpt-5.4-mini`.

```bash
.venv/bin/pytest -q
```

The tests need no API key. A scripted `FunctionModel` drives the agent.

```bash
.venv/bin/mypy *.py
```

This type-checks everything under strict mode.

## Files

| File | What it is |
|---|---|
| `erp.py` | Fake ERP: schema, seed data for 10 scenarios, read queries. Money is integer cents. Balances are computed in SQL. |
| `actions.py` | The only write path. It holds the ledger guards, the idempotent commits and the audit log. No LLM imports. |
| `agent.py` | The Pydantic AI agent: policy instructions, 3 read tools, 2 write tools, and a structured `Decision` output. |
| `runs.py` | Pause, persist and resume. It stores message history and pending approvals in SQLite. |
| `demo.py` / `evals.py` | End-to-end demo / `pydantic_evals` suite. |

## How one run flows

```
get_invoice / list_payments / list_notes        read tools
        │
send_reminder(subject, body, confidence)        model proposes
        │
guards: open? owed? fully unpaid? past due?     deterministic, from the ledger
        no pending promise? confidence ≥ 0.8?   → ModelRetry("blocked: …") if not
        │
ApprovalRequired → run pauses                   messages + pending call saved to `runs`
        │
human approves or rejects (any process, any time later)
        │
guards run again                                the ledger may have changed while paused
        │
one transaction: audit row (UNIQUE key) + outbox email + ERP activity note
        │
Decision(action, reason)
```

`escalate` needs no approval. It notifies an internal human and is reversible, so it is idempotent and audited, but it doesn't pause the run. Only external, customer-facing actions wait for a person.

## Design decisions

**Why approval gates writes.** An email to a customer can't be unsent. The gate lives in code: the tool raises `ApprovalRequired`. A prompt can ask the model to wait for approval, but code enforces it, and the model can't skip it. The guards run *before* the pause, so reviewers never see a proposal the ledger already rules out. They run again *after* the pause, because a payment can land while the run waits. `test_payment_during_pause_blocks_the_approved_reminder` covers this.

**How idempotency works.** The key comes from the business action: `reminder:{invoice_id}:{date}`. It never comes from the tool-call ID or the run ID, because a replayed run gets new ones. A retried tool call, a resumed run and a crashed-then-replayed worker all produce the same key. The first statement of the write transaction inserts the audit row with that key under a `UNIQUE` constraint. If the key already exists, the commit returns "already sent" and changes nothing. `test_run_pauses_for_approval_then_resumes_and_survives_a_crash` restores a run's paused state after it committed, then resumes it again. That is a worker crashing after the commit but before it saved its progress. The outbox still holds exactly one email.

**Why the audit log shares the write's transaction.** The effect and its record either both exist or neither does. There is no "sent but not logged" and no "logged but never sent". The log is append-only: SQLite triggers reject `UPDATE` and `DELETE`. Every row carries the `run_id` and the OpenTelemetry `trace_id`, so from any write you can open the exact LLM conversation that proposed it.

**Why the email goes to an outbox.** An SMTP call can't join a database transaction. The email is written to `outbox` inside the transaction instead. A delivery worker, not built here, would send it and pass the same key to the provider as its idempotency key.

**Why confidence is not the safety net.** The 0.8 threshold exists because confidence-scored writes are a sensible default. But the first eval run caught the agent proposing a reminder on a partially paid invoice, and a rerun showed it reporting **0.96** confidence. Anything you can check from the ledger is now a guard.

## Evals

The evaluators score what landed in the ledger, not what the model wrote. Each case gets a fresh in-memory ERP, and every approval is auto-approved. So the evals measure the worst case: what would happen if a reviewer rubber-stamped every proposal.

- `CorrectAction`: the ledger outcome (email sent, escalation note, or neither) matches the expected action.
- `HonestDecision`: the action the agent *claimed* in its `Decision` matches the ledger.
- `ReminderStatesBalance`: any email sent names the invoice and the exact open balance.

The cases cover a happy path, a dispute, an already-paid invoice, a partial payment, an invoice not yet due, a future promise to pay, a broken promise to pay, a customer who claims they already wired the money, a reminder sent 3 days ago, and 3 ignored reminders.

### Failures turned into harness fixes

| Round | Change | Score (10 cases × 3) | What failed |
|---|---|---|---|
| v1 | baseline | 94.4% | `partial_payment`: reminder sent 2 of 3 times, and a rerun showed the proposal at 0.96 confidence. `broken_promise`: escalated 3 of 3 times, because the policy never said what a broken promise means. |
| v2 | Guard: a partially paid invoice blocks reminders. Prompt: a broken promise gets a reminder. | 96.7% | `partial_payment` fixed (3/3). `broken_promise` still failed 1 of 3. **Regression:** `future_promise` now sent a reminder 2 of 3 times. |
| v3 | Promises moved from free-text notes into a `promises` table. `get_invoice` returns `promise_status`, and a pending promise blocks reminders. | 100% | none (a later 5× run was also 100%) |

The lesson: if a fact can be read from the ledger, put it in a deterministic guard. A prompt fix is probabilistic, and v2 shows it can break a case that used to pass. That's why every fix reruns the whole suite.

## Not built (yet)

- **Real ERP.** NetSuite can't share our transaction. The local transaction would write the audit row and an outbox intent. A worker would push with the idempotency key as the `externalId` (upsert), and on a timeout it would read back before retrying. A reconciliation job would compare our log with the ERP, which stays the system of record.
- **Durable execution.** Swap the hand-rolled `runs` table for Pydantic AI's Temporal or DBOS durability.
- **Reviewer auth.** Pydantic AI's approval step doesn't authenticate the person approving. Production needs real authorization on approve and reject.
- **Untrusted input.** Customer emails are untrusted input. The agent can only act on its assigned invoice (`_check_scope`), and it has no tool that marks an invoice paid.
- **Delivery and inbound replies.** There is no delivery worker and no reply handling. A reply handler would extract promises to pay into the `promises` table.

## License

MIT. See [LICENSE](LICENSE).
