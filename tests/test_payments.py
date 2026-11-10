"""Functional, accounting, risk, review, and concurrency regression tests."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture
def client(tmp_path: Path):
    with TestClient(create_app(tmp_path / "test.sqlite3", seed_demo=False, auto_worker=False)) as session:
        yield session


def payload(key="test-payment-123", sender="acct_ava", recipient="acct_noah", amount="25.00"):
    return {"sender_id":sender,"recipient_id":recipient,"amount":amount,
            "note":"Test transfer", "idempotency_key":key}


def balance(client, account):
    return client.get(f"/api/demo/balance/{account}").json()["balance_pence"]


def tick(client):
    response=client.post("/api/demo/worker/tick")
    assert response.status_code == 200
    return response.json()


def test_submit_is_pending_until_worker_runs(client):
    response=client.post("/api/payments",json=payload())
    assert response.status_code==202
    assert response.json()["status"]=="pending"
    assert balance(client,"acct_ava")==100000
    tick(client)
    assert client.get(f"/api/payments/{response.json()['id']}").json()["status"]=="approved"
    assert balance(client,"acct_ava")==97500


def test_double_entry_is_balanced_and_auditable(client):
    pid=client.post("/api/payments",json=payload()).json()["id"]
    tick(client)
    ledger=client.get(f"/api/payments/{pid}/ledger").json()
    assert len(ledger)==2
    assert sorted(e["delta_pence"] for e in ledger)==[-2500,2500]
    assert sum(e["delta_pence"] for e in ledger)==0
    audit=client.get(f"/api/payments/{pid}/audit").json()
    assert [event["event_type"] for event in audit]==["payment.created","payment.approved"]
    assert client.get("/api/reconciliation").json()["ok"] is True


def test_idempotent_retry_and_conflicting_payload(client):
    original=client.post("/api/payments",json=payload()).json()
    duplicate=client.post("/api/payments",json=payload()).json()
    assert duplicate["id"]==original["id"]
    assert duplicate["replayed"] is True
    assert client.post("/api/payments",json=payload(amount="26.00")).status_code==409
    tick(client)
    replay=client.post("/api/payments",json=payload()).json()
    assert replay["status"]=="approved"
    assert balance(client,"acct_ava")==97500
    assert client.get("/api/stats").json()["total"]==1


def test_pending_survives_restart(tmp_path: Path):
    path=tmp_path/"restart.sqlite3"
    with TestClient(create_app(path,seed_demo=False,auto_worker=False)) as c:
        id1=c.post("/api/payments",json=payload()).json()["id"]
    with TestClient(create_app(path,seed_demo=False,auto_worker=False)) as c:
        assert c.get(f"/api/payments/{id1}").json()["status"]=="pending"
        tick(c)
        assert c.get(f"/api/payments/{id1}").json()["status"]=="approved"
        assert c.get("/api/reconciliation").json()["ok"]


def test_flagged_transfer_holds_money_until_approved(client):
    request=payload(key="large-transaction-1",sender="acct_arjun",recipient="acct_mia",amount="2400.00")
    pid=client.post("/api/payments",json=request).json()["id"]
    tick(client)
    assert client.get(f"/api/payments/{pid}").json()["status"]=="flagged"
    assert balance(client,"acct_arjun")==335000
    assert client.get(f"/api/payments/{pid}/ledger").json()==[]
    assert [p["id"] for p in client.get("/api/reviews").json()]==[pid]
    reviewed=client.post(f"/api/reviews/{pid}",json={"decision":"approve","note":"Fictional demo override"})
    assert reviewed.status_code==200
    assert reviewed.json()["status"]=="approved"
    assert balance(client,"acct_arjun")==95000
    assert len(client.get(f"/api/payments/{pid}/ledger").json())==2
    assert client.post(f"/api/reviews/{pid}",json={"decision":"approve","note":"Again"}).status_code==409
    assert client.get("/api/reconciliation").json()["ok"]


def test_reject_flagged_payment_no_ledger(client):
    pid=client.post("/api/payments",json=payload(key="reject-test-101",sender="acct_arjun",amount="2450")).json()["id"]
    tick(client)
    response=client.post(f"/api/reviews/{pid}",json={"decision":"reject","note":"Risk review demo"})
    assert response.json()["status"]=="declined"
    assert balance(client,"acct_arjun")==335000
    assert client.get(f"/api/payments/{pid}/ledger").json()==[]
    assert client.get("/api/reconciliation").json()["ok"]


def test_review_approval_rechecks_balance(client):
    high=client.post("/api/payments",json=payload(key="review-needs-funds",sender="acct_arjun",recipient="acct_noah",amount="3000")).json()["id"]
    tick(client)
    # Spend much of the available balance with two small approved payments.
    # £500 is sufficient to make a later £3,000 review unaffordable.
    client.post("/api/payments",json=payload(key="small-spend-001",sender="acct_arjun",recipient="acct_mia",amount="500"))
    tick(client)
    assert balance(client,"acct_arjun")==285000
    reviewed=client.post(f"/api/reviews/{high}",json={"decision":"approve","note":"Try release"})
    assert reviewed.json()["status"]=="declined"
    assert client.get(f"/api/payments/{high}/ledger").json()==[]
    assert client.get("/api/reconciliation").json()["ok"]


def test_insufficient_balance_is_declined_without_entries(client):
    pid=client.post("/api/payments",json=payload(amount="1100")).json()["id"]
    tick(client)
    assert client.get(f"/api/payments/{pid}").json()["status"]=="declined"
    assert client.get(f"/api/payments/{pid}/ledger").json()==[]
    assert balance(client,"acct_ava")==100000


def test_validation_and_not_found(client):
    assert client.post("/api/payments",json=payload(amount="0")).status_code==422
    assert client.post("/api/payments",json=payload(amount="1.001")).status_code==422
    assert client.post("/api/payments",json=payload(recipient="acct_ava")).status_code==422
    assert client.post("/api/payments",json=payload(recipient="bad-id")).status_code==422
    assert client.get("/api/payments/nonexistent").status_code==404
    assert client.get("/api/demo/balance/nonexistent").status_code==404


def test_velocity_flags_fourth(client):
    for idx in range(3):
        client.post("/api/payments",json=payload(key=f"velocity-rule-{idx}"))
        tick(client)
    last=client.post("/api/payments",json=payload(key="velocity-fourth"))
    tick(client)
    processed=client.get(f"/api/payments/{last.json()['id']}").json()
    assert processed["status"]=="flagged"
    assert any("velocity" in x.lower() for x in processed["risk_reasons"])


def test_two_simultaneous_payments_only_one_can_spend(client):
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda i: client.post("/api/payments",json=payload(key=f"parallel-spend-{i}",amount="700")),range(2)))
    assert all(res.status_code==202 for res in results)
    tick(client)
    statuses=sorted(client.get(f"/api/payments/{res.json()['id']}").json()["status"] for res in results)
    assert statuses==["approved","declined"]
    assert balance(client,"acct_ava")==30000
    assert client.get("/api/reconciliation").json()["ok"]


def test_same_key_concurrent_only_one_record(client):
    with ThreadPoolExecutor(max_workers=4) as pool:
        results=list(pool.map(lambda _: client.post("/api/payments",json=payload()),range(4)))
    ids={r.json()["id"] for r in results}
    assert len(ids)==1
    assert [r.json()["replayed"] for r in results].count(False)==1
    tick(client)
    assert client.get("/api/stats").json()["total"]==1
    assert len(client.get(f"/api/payments/{ids.pop()}/ledger").json())==2


def test_events_are_transactionally_created_and_simulated_delivery(client):
    pid=client.post("/api/payments",json=payload()).json()["id"]
    events=client.get("/api/events").json()
    assert len(events)==1 and events[0]["delivery_status"]=="queued"
    tick(client)
    events=[e for e in client.get("/api/events").json() if e["payment_id"]==pid]
    assert len(events)==2
    assert all(e["delivery_status"]=="delivered_simulated" for e in events)


def test_reconciliation_detects_tampering(client, tmp_path: Path):
    # Deliberate test-only corruption: the check must notice that balances
    # no longer correspond to ledger entries.
    import sqlite3
    client.post("/api/payments",json=payload())
    tick(client)
    assert client.get("/api/reconciliation").json()["ok"]
    db_path=tmp_path/"test.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE accounts SET balance_pence=balance_pence+1 WHERE id='acct_noah'")
    report=client.get("/api/reconciliation").json()
    assert report["ok"] is False
    assert len(report["account_mismatches"])==1


def test_account_listing_does_not_disclose_balances(client):
    assert all(set(account)=={"id","name"} for account in client.get("/api/accounts").json())


def test_review_requires_reason(client):
    pid=client.post("/api/payments",json=payload(key="review-reason",sender="acct_arjun",amount="2400")).json()["id"]
    tick(client)
    assert client.post(f"/api/reviews/{pid}",json={"decision":"approve","note":""}).status_code==422


def test_accounting_rows_are_append_only(client, tmp_path: Path):
    import sqlite3
    pid=client.post("/api/payments",json=payload()).json()["id"]
    tick(client)
    with sqlite3.connect(tmp_path/"test.sqlite3") as conn:
        with pytest.raises(sqlite3.DatabaseError,match="append-only"):
            conn.execute("UPDATE ledger_entries SET delta_pence=1 WHERE payment_id=?",(pid,))
        with pytest.raises(sqlite3.DatabaseError,match="append-only"):
            conn.execute("DELETE FROM audit_events WHERE payment_id=?",(pid,))
    assert client.get("/api/reconciliation").json()["ok"]
