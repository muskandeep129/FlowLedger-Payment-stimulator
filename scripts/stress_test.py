"""Run a repeatable *local* concurrent submission test. No network services used.

Usage: python -m scripts.stress_test
Measures only local request/response times, not real-world throughput.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory
from time import perf_counter

from fastapi.testclient import TestClient
from app.main import create_app


def main():
    with TemporaryDirectory(prefix="flowledger-load-") as temp:
        app=create_app(Path(temp)/"test.sqlite3", seed_demo=False, auto_worker=False)
        with TestClient(app) as client:
            requests=80
            def submit(index: int):
                payload={"sender_id":"acct_noah","recipient_id":"acct_mia","amount":"1.00",
                         "note":"Concurrent local test", "idempotency_key":f"load-test-{index:04d}-local"}
                start=perf_counter()
                response=client.post("/api/payments",json=payload)
                return response.status_code,(perf_counter()-start)*1000,response.json()
            start=perf_counter()
            with ThreadPoolExecutor(max_workers=12) as pool:
                results=list(pool.map(submit,range(requests)))
            submit_seconds=perf_counter()-start
            assert all(code==202 for code,_,_ in results)
            ids={item["id"] for _,_,item in results}
            assert len(ids)==requests
            # All statuses begin pending. Worker drains the durable queue in batches.
            processed=0
            for _ in range(4):
                tick=client.post("/api/demo/worker/tick").json()
                processed+=tick["processed"]
            assert processed==requests
            statuses=client.get("/api/stats").json()
            reconciliation=client.get("/api/reconciliation").json()
            assert reconciliation["ok"],reconciliation
            assert statuses["total"]==requests and statuses["pending"]==0
            timings=sorted(ms for _,ms,_ in results)
            p95=timings[min(len(timings)-1,int(.95*(len(timings)-1)))]
            print("FLOWLEDGER LOCAL STRESS TEST")
            print(f"{requests} concurrent requests / 12 threads · {submit_seconds:.2f} seconds total")
            print(f"Submit latency: p50 {median(timings):.1f} ms · p95 {p95:.1f} ms")
            print("Payment statuses:",statuses)
            print("Reconciliation:", "PASS" if reconciliation["ok"] else "FAIL")
            print("These measurements are from an in-process local demo, not a production benchmark.")


if __name__=="__main__":
    main()
