# FlowLedger 2.0: Guided technical walkthrough

This is intended as a portfolio **learning exercise**, not a claim of having built a production payment processor.

## 1. Understand the architecture

1. `app/main.py`: REST endpoints, request validation, and the app's lifespan thread.
2. `app/database.py` -> `initialize`: the SQL schema, indexes, append-only triggers.
3. `submit_payment`: idempotency check before creating a durable `pending` payment.
4. `process_pending`: worker applies risk scoring and fund checks, then either completes, flags or declines.
5. `_transfer`: both account updates and both ledger entries are committed atomically.
6. `review_payment`: flagged transfers can be rejected or conditionally approved after balance recheck.
7. `reconcile`: validates two-sided transfers, account balances and overall total.
8. `dispatch_outbox`: event intents are recorded transactionally but delivery is **simulated**.

## 2. Example payment flow

- Suppose Ava has £1,000 and sends Noah £25.
- Browser sends `POST /api/payments` with an idempotency key.
- The API records `pending` plus a `payment.created` event in a SQL transaction.
- The worker checks risk, then records a debit of **-2500 pence** and a credit of **+2500 pence**.
- Account balances are updated atomically to £975 and Noah's prior balance + £25.
- The payment changes to `approved`, and audit/outbox events are appended in the same transaction.
- The frontend polls and displays the new status. A retry with the same key cannot charge again.

## 3. Concurrency and transaction isolation

`BEGIN IMMEDIATE` reserves the SQLite writer lock before each payment is read and settled. This prevents two worker transactions from independently spending the same old funds. In SQLite, writes serialize; this is intentionally not a scalable multi-service architecture.

Automated tests submit two separate £700 payments from an account with only £1,000. After the worker processes the queue, **one should be approved and the other declined**. A different test sends the same idempotency key through four concurrent requests, and only one payment record is created.

## 4. Accounting invariant

For every approved transfer of `amount_pence`, there must be:

- Sender ledger delta = `-amount_pence`.
- Recipient ledger delta = `+amount_pence`.
- Net ledger delta = 0.
- Cached account balance = initial account balance + sum of ledger deltas.

The reconciliation API checks all of these. A deliberate single-penny balance alteration in the test is detected.

## 5. Risk, review and events

The risk score is **not** the statistical probability of fraud. It is a fixed teaching rule. A flagged transfer stays unsettled until a demo analyst chooses approve or reject. Approving re-checks the current balance, because other payments could have spent those funds in the meantime.

The transactional outbox is about reliability: state changes and messages describing them commit together. This toy's event worker **only marks them as delivered locally**. Don't claim it sends real webhooks.

## 6. Explain it to a recruiter

> FlowLedger is my local payment infrastructure simulation built with Python, FastAPI and SQLite. I focused on durable payment states, idempotent requests, safe concurrent transfers and a double-entry ledger. I added risk-based review, a transactional event outbox and reconciliation checks, then covered the main invariants with automated tests. It is a learning project using entirely fictional data, rather than a production fintech product.

Be clear which parts you personally understand, modified or tested, and what tools assisted you in writing them.

## 7. Things to try yourself

1. Trace a sample payment through the SQL tables.
2. Add a unit test for one more risk rule.
3. Explain the difference between a request being `202 Accepted` and a payment being `approved`.
4. Change the concurrency test to three competing payments.
5. Explore why an outbox reduces inconsistencies between state changes and event records.
6. Consider how row-level locking in PostgreSQL would differ from SQLite's single-writer model.
