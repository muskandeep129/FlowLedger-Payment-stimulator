"""Durable toy payment ledger. SQLite is deliberately single-node and demo-only.

The important invariant is that an approved transfer produces exactly two
opposite-signed immutable ledger entries in the same transaction as its status
change and cached account balance updates.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

DEMO_ACCOUNTS = [
    ("acct_ava", "Ava Patel", 100_000),
    ("acct_noah", "Noah Smith", 240_000),
    ("acct_mia", "Mia Chen", 185_000),
    ("acct_arjun", "Arjun Singh", 335_000),
]
RISK_THRESHOLD = 60


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class IdempotencyConflict(Exception):
    """Key is already associated with another request payload."""


class InvalidPayment(Exception):
    """Invalid transfer request."""


class ReviewConflict(Exception):
    """Review is not possible for this payment state."""


class LedgerDB:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=15000")
        return conn

    def initialize(self, seed_demo: bool = True) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS accounts (
                  id TEXT PRIMARY KEY,
                  name TEXT NOT NULL,
                  initial_balance_pence INTEGER NOT NULL CHECK(initial_balance_pence>=0),
                  balance_pence INTEGER NOT NULL CHECK(balance_pence>=0)
                );
                CREATE TABLE IF NOT EXISTS payments (
                  id TEXT PRIMARY KEY,
                  idempotency_key TEXT UNIQUE NOT NULL,
                  sender_id TEXT NOT NULL REFERENCES accounts(id),
                  recipient_id TEXT NOT NULL REFERENCES accounts(id),
                  amount_pence INTEGER NOT NULL CHECK(amount_pence>0),
                  note TEXT NOT NULL DEFAULT '',
                  status TEXT NOT NULL CHECK(status IN ('pending','approved','flagged','declined')),
                  risk_score INTEGER NOT NULL DEFAULT 0 CHECK(risk_score BETWEEN 0 AND 100),
                  reasons_json TEXT NOT NULL DEFAULT '[]',
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_payments_time ON payments(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_payments_sender ON payments(sender_id,created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status,created_at);
                CREATE TABLE IF NOT EXISTS ledger_entries (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  payment_id TEXT NOT NULL REFERENCES payments(id),
                  account_id TEXT NOT NULL REFERENCES accounts(id),
                  delta_pence INTEGER NOT NULL CHECK(delta_pence!=0),
                  created_at TEXT NOT NULL,
                  UNIQUE(payment_id,account_id)
                );
                CREATE INDEX IF NOT EXISTS idx_ledger_account ON ledger_entries(account_id);
                CREATE TRIGGER IF NOT EXISTS ledger_no_update BEFORE UPDATE ON ledger_entries
                  BEGIN SELECT RAISE(ABORT, 'ledger entries are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS ledger_no_delete BEFORE DELETE ON ledger_entries
                  BEGIN SELECT RAISE(ABORT, 'ledger entries are append-only'); END;
                CREATE TABLE IF NOT EXISTS audit_events (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  payment_id TEXT NOT NULL REFERENCES payments(id),
                  event_type TEXT NOT NULL,
                  details_json TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_events
                  BEGIN SELECT RAISE(ABORT, 'audit events are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_events
                  BEGIN SELECT RAISE(ABORT, 'audit events are append-only'); END;
                CREATE TABLE IF NOT EXISTS outbox_events (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  payment_id TEXT NOT NULL REFERENCES payments(id),
                  event_type TEXT NOT NULL,
                  payload_json TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  delivered_at TEXT
                );
                CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            """)
            for account_id, name, balance in DEMO_ACCOUNTS:
                conn.execute("""
                    INSERT OR IGNORE INTO accounts(id,name,initial_balance_pence,balance_pence)
                    VALUES(?,?,?,?)
                """, (account_id, name, balance, balance))
            seeded = conn.execute("SELECT 1 FROM metadata WHERE key='demo_seeded'").fetchone()
            if seeded is None:
                conn.execute("INSERT INTO metadata VALUES('demo_seeded','1')")
                if seed_demo:
                    self._seed_examples()

    def _seed_examples(self) -> None:
        examples = [
            ("acct_noah", "acct_mia", 8500, "Demo: invoice payment"),
            ("acct_arjun", "acct_ava", 17500, "Demo: subscription"),
            ("acct_arjun", "acct_noah", 250000, "Demo: unusual high-value payment"),
            ("acct_ava", "acct_arjun", 125000, "Demo: insufficient funds"),
            ("acct_noah", "acct_arjun", 3500, "Demo: team lunch"),
        ]
        for i,(sender, recipient, amount, note) in enumerate(examples, 1):
            self.submit_payment(sender, recipient, amount, note, f"sample-demo-key-{i}")
        self.process_pending(limit=len(examples))
        self.dispatch_outbox(limit=100)

    @staticmethod
    def _payment_dict(row: sqlite3.Row, replayed: bool = False) -> dict[str, Any]:
        result = dict(row)
        result["risk_reasons"] = json.loads(result.pop("reasons_json"))
        result["replayed"] = replayed
        return result

    def accounts(self) -> list[dict[str, str]]:
        # Public demo picker deliberately does not expose everyone else's funds.
        with self.connect() as conn:
            return [dict(row) for row in conn.execute("SELECT id,name FROM accounts ORDER BY name")]

    def demo_balance(self, account_id: str) -> dict[str, Any] | None:
        # This is a LOCAL SANDBOX, not authentication or a production access check.
        with self.connect() as conn:
            row=conn.execute("SELECT id,name,balance_pence FROM accounts WHERE id=?",(account_id,)).fetchone()
            return dict(row) if row else None

    def payments(self, limit: int = 50, status: str | None = None) -> list[dict[str, Any]]:
        with self.connect() as conn:
            if status:
                rows=conn.execute("SELECT * FROM payments WHERE status=? ORDER BY created_at DESC LIMIT ?", (status, limit))
            else:
                rows=conn.execute("SELECT * FROM payments ORDER BY created_at DESC LIMIT ?", (limit,))
            return [self._payment_dict(row) for row in rows]

    def payment(self, payment_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row=conn.execute("SELECT * FROM payments WHERE id=?",(payment_id,)).fetchone()
            return self._payment_dict(row) if row else None

    @staticmethod
    def _append_event(conn: sqlite3.Connection, payment_id: str, event_type: str, details: dict[str, Any]) -> None:
        now=utc_now()
        payload=json.dumps(details,sort_keys=True)
        conn.execute("INSERT INTO audit_events(payment_id,event_type,details_json,created_at) VALUES(?,?,?,?)",
                     (payment_id,event_type,payload,now))
        # Transactional outbox: event intent commits with the state transition.
        conn.execute("INSERT INTO outbox_events(payment_id,event_type,payload_json,created_at) VALUES(?,?,?,?)",
                     (payment_id,event_type,payload,now))

    def submit_payment(self, sender_id: str, recipient_id: str, amount_pence: int, note: str,
                       idempotency_key: str) -> dict[str, Any]:
        if sender_id == recipient_id:
            raise InvalidPayment("Sender and recipient must be different accounts.")
        if amount_pence <= 0 or amount_pence > 5_000_000:
            raise InvalidPayment("Payment amount must be between £0.01 and £50,000.00.")
        conn=self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing=conn.execute("SELECT * FROM payments WHERE idempotency_key=?",(idempotency_key,)).fetchone()
            if existing:
                same=all((existing["sender_id"]==sender_id,
                          existing["recipient_id"]==recipient_id,
                          existing["amount_pence"]==amount_pence,
                          existing["note"]==note))
                if not same:
                    raise IdempotencyConflict("This idempotency key belongs to a different payment request.")
                conn.commit()
                return self._payment_dict(existing, replayed=True)
            known=conn.execute("SELECT COUNT(*) FROM accounts WHERE id IN (?,?)",(sender_id,recipient_id)).fetchone()[0]
            if known != 2:
                raise InvalidPayment("Both parties must be valid demo accounts.")
            pid=f"pay_{uuid.uuid4().hex[:16]}"
            now=utc_now()
            conn.execute("""INSERT INTO payments(
                id,idempotency_key,sender_id,recipient_id,amount_pence,note,status,
                risk_score,reasons_json,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,'pending',0,'[]',?,?)""",
            (pid,idempotency_key,sender_id,recipient_id,amount_pence,note,now,now))
            self._append_event(conn,pid,"payment.created",{"status":"pending"})
            row=conn.execute("SELECT * FROM payments WHERE id=?",(pid,)).fetchone()
            conn.commit()
            return self._payment_dict(row)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _risk(conn: sqlite3.Connection, payment: sqlite3.Row) -> tuple[int,list[str]]:
        score=0
        reasons=[]
        if payment["amount_pence"] >= 200_000:
            score+=65
            reasons.append("Large payment (at least £2,000)")
        prior=conn.execute("""SELECT 1 FROM payments WHERE sender_id=? AND recipient_id=?
                    AND status='approved' LIMIT 1""", (payment["sender_id"],payment["recipient_id"])).fetchone()
        if prior is None:
            score+=15
            reasons.append("First approved payment to this recipient")
        cutoff=(datetime.fromisoformat(payment["created_at"]) - timedelta(seconds=60)).isoformat(timespec="microseconds")
        # Excludes current attempt; only prior attempts within 60 s of submission.
        count=conn.execute("""SELECT COUNT(*) FROM payments WHERE sender_id=? AND created_at>=?
            AND created_at<=? AND id!=?""",(payment["sender_id"],cutoff,payment["created_at"],payment["id"])).fetchone()[0]
        if count>=3:
            score+=65
            reasons.append("High velocity: at least 3 other attempts in 60 seconds")
        return min(100,score),reasons or ["No risk rules triggered"]

    @staticmethod
    def _transfer(conn: sqlite3.Connection, payment: sqlite3.Row) -> None:
        """Always called inside BEGIN IMMEDIATE with an already-affordable balance."""
        amt=payment["amount_pence"]
        sender=payment["sender_id"]
        receiver=payment["recipient_id"]
        cursor=conn.execute("""UPDATE accounts SET balance_pence=balance_pence-?
                            WHERE id=? AND balance_pence>=?""",(amt,sender,amt))
        if cursor.rowcount != 1:
            raise RuntimeError("Account balance changed unexpectedly")
        conn.execute("UPDATE accounts SET balance_pence=balance_pence+? WHERE id=?",(amt,receiver))
        now=utc_now()
        conn.executemany("""INSERT INTO ledger_entries(payment_id,account_id,delta_pence,created_at)
                          VALUES(?,?,?,?)""",[(payment["id"],sender,-amt,now),
                                                 (payment["id"],receiver,amt,now)])

    def process_pending(self, limit: int = 20) -> list[dict[str, Any]]:
        if not 1<=limit<=200:
            raise ValueError("limit must be between 1 and 200")
        outputs=[]
        # One short transaction per payment. A crash before commit leaves it pending.
        for _ in range(limit):
            conn=self.connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                payment=conn.execute("SELECT * FROM payments WHERE status='pending' ORDER BY created_at,id LIMIT 1").fetchone()
                if payment is None:
                    conn.commit()
                    break
                score,reasons=self._risk(conn,payment)
                balance=conn.execute("SELECT balance_pence FROM accounts WHERE id=?",(payment["sender_id"],)).fetchone()[0]
                if payment["amount_pence"] > balance:
                    status="declined"
                    reasons.append("Insufficient demo funds")
                elif score>=RISK_THRESHOLD:
                    status="flagged"
                    reasons.append("Held for manual demo analyst review")
                else:
                    self._transfer(conn,payment)
                    status="approved"
                conn.execute("UPDATE payments SET status=?,risk_score=?,reasons_json=?,updated_at=? WHERE id=?",
                             (status,score,json.dumps(reasons),utc_now(),payment["id"]))
                self._append_event(conn,payment["id"],f"payment.{status}",{"status":status,"risk_score":score})
                saved=conn.execute("SELECT * FROM payments WHERE id=?",(payment["id"],)).fetchone()
                conn.commit()
                outputs.append(self._payment_dict(saved))
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
        return outputs

    def review_payment(self, payment_id: str, decision: str, note: str, reviewer: str = "demo-analyst") -> dict[str, Any]:
        if decision not in ("approve","reject"):
            raise InvalidPayment("Decision must be approve or reject")
        if not note.strip():
            raise InvalidPayment("A review reason is required.")
        conn=self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            payment=conn.execute("SELECT * FROM payments WHERE id=?",(payment_id,)).fetchone()
            if payment is None:
                raise InvalidPayment("Payment does not exist")
            if payment["status"] != "flagged":
                raise ReviewConflict("Only flagged payments can be reviewed once.")
            balance=conn.execute("SELECT balance_pence FROM accounts WHERE id=?",(payment["sender_id"],)).fetchone()[0]
            if decision=="approve" and balance>=payment["amount_pence"]:
                self._transfer(conn,payment)
                status="approved"
            elif decision=="approve":
                status="declined"
            else:
                status="declined"
            reasons=json.loads(payment["reasons_json"])
            reasons.append("Analyst approved release" if status=="approved" else
                           "Analyst approval failed: insufficient demo funds" if decision=="approve" else
                           "Rejected during analyst review")
            conn.execute("UPDATE payments SET status=?,reasons_json=?,updated_at=? WHERE id=?",
                         (status,json.dumps(reasons),utc_now(),payment_id))
            self._append_event(conn,payment_id,"review.completed",{
                "decision":decision,"resulting_status":status,"reviewer":reviewer,"note":note.strip()})
            self._append_event(conn,payment_id,f"payment.{status}",{
                "status":status,"source":"analyst_review"})
            saved=conn.execute("SELECT * FROM payments WHERE id=?",(payment_id,)).fetchone()
            conn.commit()
            return self._payment_dict(saved)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def dispatch_outbox(self, limit: int = 50) -> int:
        """Simulate webhook delivery to a local event log, never an external URL."""
        conn=self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            items=conn.execute("SELECT id FROM outbox_events WHERE delivered_at IS NULL ORDER BY id LIMIT ?",(limit,)).fetchall()
            for item in items:
                conn.execute("UPDATE outbox_events SET delivered_at=? WHERE id=? AND delivered_at IS NULL",
                             (utc_now(),item["id"]))
            conn.commit()
            return len(items)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def events(self, limit: int = 40) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute("""SELECT id,payment_id,event_type,created_at,delivered_at,
                CASE WHEN delivered_at IS NULL THEN 'queued' ELSE 'delivered_simulated' END AS delivery_status
                FROM outbox_events ORDER BY id DESC LIMIT ?""",(limit,))]

    def audit(self, payment_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [{**dict(row),"details":json.loads(row["details_json"])} for row in conn.execute("""
                SELECT * FROM audit_events WHERE payment_id=? ORDER BY id""",(payment_id,))]

    def ledger(self, payment_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute("""SELECT id,payment_id,account_id,delta_pence,created_at
                FROM ledger_entries WHERE payment_id=? ORDER BY id""",(payment_id,))]

    def stats(self) -> dict[str, Any]:
        with self.connect() as conn:
            counts=conn.execute("""SELECT COUNT(*) total,
                COALESCE(SUM(status='pending'),0) pending,
                COALESCE(SUM(status='approved'),0) approved,
                COALESCE(SUM(status='flagged'),0) flagged,
                COALESCE(SUM(status='declined'),0) declined,
                COALESCE(SUM(CASE WHEN status='approved' THEN amount_pence ELSE 0 END),0) volume_pence
                FROM payments""").fetchone()
        return dict(counts)

    def reconcile(self) -> dict[str, Any]:
        with self.connect() as conn:
            imbalanced=[dict(row) for row in conn.execute("""SELECT payment_id,SUM(delta_pence) AS net_pence,
                COUNT(*) entry_count FROM ledger_entries GROUP BY payment_id
                HAVING net_pence!=0 OR entry_count!=2""")]
            mismatched=[dict(row) for row in conn.execute("""SELECT a.id, a.balance_pence,
                a.initial_balance_pence+COALESCE(SUM(l.delta_pence),0) AS ledger_balance_pence
                FROM accounts a LEFT JOIN ledger_entries l ON l.account_id=a.id
                GROUP BY a.id HAVING a.balance_pence!=ledger_balance_pence""")]
            bad_approved=[dict(row) for row in conn.execute("""SELECT p.id FROM payments p
                LEFT JOIN ledger_entries l ON l.payment_id=p.id
                GROUP BY p.id HAVING
                  (p.status='approved' AND (
                    COUNT(l.id)!=2 OR COALESCE(SUM(l.delta_pence),0)!=0 OR
                    SUM(CASE WHEN l.account_id=p.sender_id AND l.delta_pence=-p.amount_pence THEN 1 ELSE 0 END)!=1 OR
                    SUM(CASE WHEN l.account_id=p.recipient_id AND l.delta_pence=p.amount_pence THEN 1 ELSE 0 END)!=1
                  )) OR (p.status!='approved' AND COUNT(l.id)!=0)""")]
            balance_total=conn.execute("SELECT SUM(balance_pence) FROM accounts").fetchone()[0]
            initial_total=conn.execute("SELECT SUM(initial_balance_pence) FROM accounts").fetchone()[0]
            return {
                "ok": not (imbalanced or mismatched or bad_approved) and balance_total==initial_total,
                "imbalanced_transfers":imbalanced,"account_mismatches":mismatched,
                "payment_state_errors":bad_approved,
                "current_total_pence":balance_total,"initial_total_pence":initial_total,
            }
