"""Many agents reading and writing shared memory at once.

    python scripts/concurrency.py [--threads 16] [--ops 200]

1. Claim race: 30 agents grab the same ticket at the same instant; exactly one may win.
2. Load: N threads mix memory writes (MERGE) and recalls (multi-hop reads) across two tenants.
3. Isolation: nothing written to one tenant's graph is visible in the other's.
"""
from __future__ import annotations

import argparse
import random
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from recalldesk import db, memory, seed  # noqa: E402

A, B = "load-a", "load-b"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--threads", type=int, default=16)
    ap.add_argument("--ops", type=int, default=200, help="operations per thread")
    args = ap.parse_args()

    seed.seed_tenant(A, seed.TENANTS["acme"])
    seed.seed_tenant(B, seed.TENANTS["globex"])

    # 1. claim race
    sid = memory.start_session(A, "C-1001")
    ticket = memory.open_or_reuse_ticket(A, "C-1001", sid, "billing", "race", [])["id"]
    barrier = threading.Barrier(30)
    agents = ["triage", "billing", "technical"]

    def claim(i: int) -> str:
        barrier.wait()
        return memory.claim_ticket(A, ticket, agents[i % 3])

    with ThreadPoolExecutor(30) as ex:
        owners = list(ex.map(claim, range(30)))
    edges = db.run(A, "MATCH (:Ticket {id:$t})-[r:ASSIGNED_TO]->() RETURN count(r) AS n", {"t": ticket}).rows[0]["n"]
    print(f"claim race: 30 agents -> owners seen {set(owners)}, ASSIGNED_TO edges {edges}")
    assert len(set(owners)) == 1 and edges == 1, "more than one agent won the ticket"

    # 2. load
    sessions = {A: {c: memory.start_session(A, c) for c in ["C-1001", "C-1002", "C-1003", "C-1004"]},
                B: {c: memory.start_session(B, c) for c in ["C-2001", "C-2002"]}}
    lat = {"write": [], "read": []}
    lock = threading.Lock()

    def worker(w: int) -> None:
        rnd = random.Random(w)
        for i in range(args.ops):
            tenant = A if rnd.random() < 0.7 else B
            cid, sid_ = rnd.choice(list(sessions[tenant].items()))
            t0 = time.perf_counter()
            if rnd.random() < 0.5:
                memory.remember(tenant, cid, sid_, rnd.choice(agents), "context", f"load fact {w}-{i % 20}")
                kind = "write"
            else:
                memory.recall(tenant, cid)
                kind = "read"
            with lock:
                lat[kind].append((time.perf_counter() - t0) * 1000)

    t0 = time.perf_counter()
    with ThreadPoolExecutor(args.threads) as ex:
        list(ex.map(worker, range(args.threads)))
    wall = time.perf_counter() - t0
    total = sum(len(v) for v in lat.values())
    print(f"load: {args.threads} threads, {total} ops in {wall:.2f}s = {total / wall:.0f} ops/s")
    for k, v in lat.items():
        v.sort()
        print(f"  {k:5}: n={len(v)} p50={statistics.median(v):.1f}ms p95={v[int(len(v) * .95)]:.1f}ms")
    dupes = db.run(A, """MATCH (c:Customer)-[:HAS_MEMORY]->(m:Memory)
                         WITH c, m.text AS t, count(*) AS n WHERE n > 1 RETURN count(*) AS d""").rows[0]["d"]
    print(f"  duplicate memories after concurrent MERGE: {dupes}")
    assert dupes == 0

    # 3. isolation: same person (priya@example.com) exists in both tenants
    memory.remember(A, "C-1001", sessions[A]["C-1001"], "triage", "preference", "Secret only Acme should know")
    leaked = db.run(B, "MATCH (m:Memory {text:'Secret only Acme should know'}) RETURN count(m) AS n").rows[0]["n"]
    print(f"isolation: memory written to {A} visible in {B}: {leaked}")
    assert leaked == 0

    for t in (A, B):
        db.graph_for(t).delete()
    print("all checks passed")


if __name__ == "__main__":
    main()
