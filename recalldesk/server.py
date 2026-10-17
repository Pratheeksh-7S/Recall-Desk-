"""HTTP API and web UI.  Run:  uvicorn recalldesk.server:app --port 8000"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import agents, db, llm, memory, seed

STATIC = Path(__file__).resolve().parent.parent / "static"
app = FastAPI(title="Recall Desk")


@app.on_event("startup")
def ensure_seeded() -> None:
    existing = set(db.list_tenants())
    for tenant, spec in seed.TENANTS.items():
        if tenant not in existing:
            seed.seed_tenant(tenant, spec)


class SessionIn(BaseModel):
    tenant: str
    customer_id: str


class ChatIn(SessionIn):
    session_id: str
    message: str


class FeedbackIn(BaseModel):
    tenant: str
    ticket_id: str
    resolved: bool


def _tenant(tenant: str) -> str:
    if tenant not in seed.TENANTS:
        raise HTTPException(404, f"unknown tenant {tenant}")
    return tenant


@app.get("/api/config")
def config():
    return {"provider": llm.provider(), "model": llm.model_name()}


@app.get("/api/tenants")
def tenants():
    out = []
    for t, spec in seed.TENANTS.items():
        rows = db.run(t, """
            MATCH (c:Customer)-[:SUBSCRIBES_TO]->(p:Plan), (c)-[:IN_REGION]->(r:Region)
            RETURN c.id AS id, c.name AS name, p.name AS plan, r.name AS region ORDER BY id
        """, read_only=True).rows
        out.append({"id": t, "company": spec["company"], "customers": rows})
    return out


@app.post("/api/session")
def new_session(body: SessionIn):
    t = _tenant(body.tenant)
    sid = memory.start_session(t, body.customer_id)
    return {"session_id": sid, **agents.greet(t, body.customer_id, sid)}


@app.post("/api/chat")
def chat(body: ChatIn):
    t = _tenant(body.tenant)
    if not body.message.strip():
        raise HTTPException(400, "empty message")
    return agents.handle_message(t, body.session_id, body.customer_id, body.message.strip()[:2000])


@app.post("/api/feedback")
def feedback(body: FeedbackIn):
    out = memory.record_outcome(_tenant(body.tenant), body.ticket_id, body.resolved)
    if not out:
        raise HTTPException(404, "unknown ticket")
    return out


@app.get("/api/memory/{tenant}/{customer_id}")
def memory_graph(tenant: str, customer_id: str):
    """The customer's memory neighbourhood: sessions, memories, tickets, owners, handoffs, causes."""
    r = db.run(_tenant(tenant), """
        MATCH p=(c:Customer {id:$cid})-[:HAS_MEMORY|OPENED]->(x)
        OPTIONAL MATCH q=(x)-[:ASSIGNED_TO|CAUSED_BY|USED_PROCEDURE|WRITTEN_BY|ABOUT]->(y)
        OPTIONAL MATCH h=(x)<-[:FOR]-(:Handoff)-[:TO]->(:Agent)
        RETURN p, q, h
        UNION
        MATCH p=(c:Customer {id:$cid})<-[:WITH]-(s:Session)
        OPTIONAL MATCH q=(s)-[:FOLLOWED_BY]->(:Session)
        OPTIONAL MATCH h=(s)<-[:LEARNED_IN]-(:Memory)
        RETURN p, q, h
    """, {"cid": customer_id}, read_only=True)
    rec = agents._recall_dict(memory.recall(tenant, customer_id))
    return {"graph": {"nodes": list(r.nodes.values()), "edges": list(r.edges.values())}, "memory": rec,
            "cypher": r.cypher}


@app.post("/api/reset")
def reset():
    seed.seed_all()
    return {"ok": True}


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")
