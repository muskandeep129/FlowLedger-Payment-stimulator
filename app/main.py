"""FlowLedger API: a LOCAL simulated payments platform, not a live money service."""
from __future__ import annotations

import logging
import os
import threading
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.database import (
    IdempotencyConflict, InvalidPayment, LedgerDB, ReviewConflict, RISK_THRESHOLD,
)

ROOT = Path(__file__).resolve().parent
logger = logging.getLogger(__name__)


class PaymentRequest(BaseModel):
    sender_id: str = Field(min_length=1, max_length=100)
    recipient_id: str = Field(min_length=1, max_length=100)
    amount: Decimal = Field(gt=0, le=50_000, description="Simulated GBP amount with max 2 decimal places")
    note: str = Field(default="", max_length=120)
    idempotency_key: str = Field(min_length=8, max_length=100)


class ReviewRequest(BaseModel):
    decision: Literal["approve", "reject"]
    note: str = Field(min_length=2, max_length=200)


def create_app(db_path: str | Path | None = None, seed_demo: bool = True,
               auto_worker: bool = True) -> FastAPI:
    path = db_path or os.environ.get("FLOWLEDGER_DB_PATH", "data/flowledger.sqlite3")
    db = LedgerDB(path)
    stop = threading.Event()

    def run_worker():
        while not stop.wait(0.75):
            try:
                db.process_pending(limit=20)
                db.dispatch_outbox(limit=100)
            except Exception:
                logger.exception("Demo worker tick failed; queued payments are retained")

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        db.initialize(seed_demo=seed_demo)
        stop.clear()
        worker = None
        if auto_worker:
            worker = threading.Thread(target=run_worker, name="flowledger-demo-worker", daemon=True)
            worker.start()
        try:
            yield
        finally:
            stop.set()
            if worker:
                worker.join(timeout=5)

    app=FastAPI(
        title="FlowLedger | Payment Infrastructure Simulator",
        description="Local-only fictional payment processing with idempotent queues, immutable ledger entries, review and outbox events.",
        version="2.0.0",
        lifespan=lifespan,
    )

    @app.get("/api/health")
    def health():
        return {"status":"ok", "mode":"local-simulation", "auto_worker":auto_worker}

    @app.get("/api/accounts")
    def accounts():
        return db.accounts()

    @app.get("/api/demo/balance/{account_id}")
    def demo_balance(account_id: str):
        # The dropdown can select any fictional account. This is NOT an auth boundary.
        data=db.demo_balance(account_id)
        if data is None:
            raise HTTPException(404,"Demo account not found")
        return data

    @app.get("/api/payments")
    def payments(limit: Annotated[int, Query(ge=1,le=100)] = 50):
        return db.payments(limit)

    @app.get("/api/payments/{payment_id}")
    def payment(payment_id: str):
        item=db.payment(payment_id)
        if item is None:
            raise HTTPException(404,"Payment not found")
        return item

    @app.get("/api/payments/{payment_id}/ledger")
    def payment_ledger(payment_id: str):
        if not db.payment(payment_id):
            raise HTTPException(404,"Payment not found")
        return db.ledger(payment_id)

    @app.get("/api/payments/{payment_id}/audit")
    def payment_audit(payment_id: str):
        if not db.payment(payment_id):
            raise HTTPException(404,"Payment not found")
        return db.audit(payment_id)

    @app.post("/api/payments", status_code=202)
    def create_payment(payload: PaymentRequest):
        pence=payload.amount*100
        if not pence.is_finite() or pence != pence.to_integral_value():
            raise HTTPException(422,"Payment amounts must have no more than two decimal places")
        try:
            return db.submit_payment(payload.sender_id,payload.recipient_id,int(pence),
                                     payload.note.strip(),payload.idempotency_key)
        except IdempotencyConflict as exc:
            raise HTTPException(409,str(exc)) from exc
        except InvalidPayment as exc:
            raise HTTPException(422,str(exc)) from exc

    @app.get("/api/reviews")
    def review_queue():
        return db.payments(limit=100,status="flagged")

    @app.post("/api/reviews/{payment_id}")
    def review_payment(payment_id: str, payload: ReviewRequest):
        try:
            return db.review_payment(payment_id,payload.decision,payload.note)
        except ReviewConflict as exc:
            raise HTTPException(409,str(exc)) from exc
        except InvalidPayment as exc:
            raise HTTPException(422,str(exc)) from exc

    @app.get("/api/events")
    def events(limit: Annotated[int,Query(ge=1,le=100)] = 30):
        return db.events(limit)

    @app.get("/api/stats")
    def stats():
        return db.stats()

    @app.get("/api/reconciliation")
    def reconciliation():
        return db.reconcile()

    @app.get("/api/rules")
    def rules():
        return {"review_threshold":RISK_THRESHOLD,
                "rules":[
                    {"name":"High-value transfer","points":65,"condition":"Amount ≥ £2,000"},
                    {"name":"First-time recipient","points":15,"condition":"No previous approved transfer"},
                    {"name":"Rapid attempts","points":65,"condition":"At least 3 other attempts in 60 seconds"},
                ],"description":"Illustrative thresholds, not real fraud modelling."}

    @app.post("/api/demo/worker/tick")
    def worker_tick():
        # Manual execution is useful for testing when automatic worker is off.
        processed=db.process_pending(limit=100)
        delivered=db.dispatch_outbox(limit=200)
        return {"processed":len(processed),"simulated_events_delivered":delivered,
                "payments":processed}

    app.mount("/static",StaticFiles(directory=ROOT/"static"),name="static")

    @app.get("/",include_in_schema=False)
    def dashboard():
        return FileResponse(ROOT/"static"/"index.html")

    return app


app=create_app()
