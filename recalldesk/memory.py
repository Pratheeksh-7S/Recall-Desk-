"""Agent memory, stored as nodes and edges in the tenant graph.

episodic   (:Session)-[:HAS_TURN]->(:Turn)-[:NEXT]->(:Turn), turns MENTION the entities they talked about
semantic   (:Customer)-[:HAS_MEMORY]->(:Memory {kind, text}) with LEARNED_IN / WRITTEN_BY / ABOUT edges
procedural (:Procedure {steps, successes, failures})-[:FOR_TOPIC]->(:Topic), ranked by outcome
shared     (:Ticket)-[:ASSIGNED_TO]->(:Agent), (:Handoff)-[:FROM|TO|FOR|CARRIES]->(...)
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from . import db


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


# --- episodic --------------------------------------------------------------

def start_session(tenant: str, customer_id: str) -> str:
    sid = _id("S")
    db.run(tenant, """
        MATCH (c:Customer {id:$cid})
        CREATE (s:Session {id:$sid, started:$ts})-[:WITH]->(c)
        WITH s, c
        OPTIONAL MATCH (c)<-[:WITH]-(prev:Session) WHERE prev.id <> $sid
        WITH s, prev ORDER BY prev.started DESC LIMIT 1
        FOREACH (_ IN CASE WHEN prev IS NULL THEN [] ELSE [1] END | CREATE (prev)-[:FOLLOWED_BY]->(s))
    """, {"cid": customer_id, "sid": sid, "ts": _now()})
    return sid


def add_turn(tenant: str, session_id: str, role: str, agent: str, text: str, mentions: list[str] | None = None) -> str:
    """Append a turn to the session chain and link it to the graph entities it mentioned."""
    tid = _id("T")
    db.run(tenant, """
        MATCH (s:Session {id:$sid})
        OPTIONAL MATCH (s)-[:HAS_TURN]->(last:Turn) WHERE NOT (last)-[:NEXT]->(:Turn)
        CREATE (s)-[:HAS_TURN]->(t:Turn {id:$tid, role:$role, agent:$agent, text:$text, ts:$ts})
        FOREACH (_ IN CASE WHEN last IS NULL THEN [] ELSE [1] END | CREATE (last)-[:NEXT]->(t))
        WITH t
        UNWIND $mentions AS m
        MATCH (x) WHERE (x:Feature AND x.name = m) OR (x:Invoice AND x.id = m) OR (x:Incident AND x.id = m)
                     OR (x:Service AND x.name = m) OR (x:Article AND x.id = m)
        MERGE (t)-[:MENTIONS]->(x)
    """, {"sid": session_id, "tid": tid, "role": role, "agent": agent, "text": text, "ts": _now(),
          "mentions": mentions or []})
    return tid


def session_turns(tenant: str, session_id: str, limit: int = 12) -> list[dict]:
    r = db.run(tenant, """
        MATCH (:Session {id:$sid})-[:HAS_TURN]->(t:Turn)
        RETURN t.role AS role, t.agent AS agent, t.text AS text, t.ts AS ts
        ORDER BY t.ts DESC, t.id DESC LIMIT $limit
    """, {"sid": session_id, "limit": limit}, read_only=True)
    return list(reversed(r.rows))


# --- semantic --------------------------------------------------------------

def remember(tenant: str, customer_id: str, session_id: str, agent: str, kind: str, text: str,
             about: str | None = None) -> str | None:
    """Store a fact about the customer. Identical facts are merged, not duplicated."""
    mid = _id("M")
    r = db.run(tenant, """
        MATCH (c:Customer {id:$cid}), (s:Session {id:$sid}), (a:Agent {name:$agent})
        MERGE (c)-[:HAS_MEMORY]->(m:Memory {kind:$kind, text:$text})
        ON CREATE SET m.id = $mid, m.created = $ts, m.hits = 1
        ON MATCH SET m.hits = m.hits + 1, m.last_seen = $ts
        MERGE (m)-[:LEARNED_IN]->(s)
        MERGE (m)-[:WRITTEN_BY]->(a)
        WITH m
        OPTIONAL MATCH (x) WHERE (x:Feature AND x.name = $about) OR (x:Invoice AND x.id = $about) OR (x:Incident AND x.id = $about)
        FOREACH (_ IN CASE WHEN x IS NULL THEN [] ELSE [1] END | MERGE (m)-[:ABOUT]->(x))
        RETURN m.id AS id
    """, {"cid": customer_id, "sid": session_id, "agent": agent, "kind": kind, "text": text,
          "about": about or "", "mid": mid, "ts": _now()})
    return r.rows[0]["id"] if r.rows else None


def recall(tenant: str, customer_id: str) -> db.QueryResult:
    """Everything the desk remembers about a customer, from any past session and any agent."""
    return db.run(tenant, """
        MATCH (c:Customer {id:$cid})
        OPTIONAL MATCH mp=(c)-[:HAS_MEMORY]->(m:Memory)-[:WRITTEN_BY]->(a:Agent)
        WITH c, collect(DISTINCT {kind:m.kind, text:m.text, by:a.name, at:m.created, hits:m.hits}) AS memories,
             collect(mp) AS mpaths
        OPTIONAL MATCH tp=(c)-[:OPENED]->(t:Ticket)-[:ASSIGNED_TO]->(owner:Agent)
        WITH c, memories, mpaths, collect(DISTINCT {id:t.id, topic:t.topic, status:t.status, summary:t.summary,
             owner:owner.name, created:t.created}) AS tickets, collect(tp) AS tpaths
        OPTIONAL MATCH (c)<-[:WITH]-(s:Session)
        RETURN [x IN memories WHERE x.text IS NOT NULL] AS memories,
               [x IN tickets WHERE x.id IS NOT NULL] AS tickets,
               count(DISTINCT s) AS sessions, mpaths, tpaths
    """, {"cid": customer_id}, read_only=True)


# --- procedural ------------------------------------------------------------

def best_procedure(tenant: str, topic: str) -> dict | None:
    """Pick the procedure for a topic with the best outcome record (Laplace-smoothed success rate)."""
    r = db.run(tenant, """
        MATCH (p:Procedure)-[:FOR_TOPIC]->(:Topic {name:$topic})
        WITH p, (p.successes + 1.0) / (p.successes + p.failures + 2.0) AS rate
        RETURN p.name AS name, p.steps AS steps, p.successes AS successes, p.failures AS failures,
               round(rate * 100) AS success_rate
        ORDER BY rate DESC LIMIT 1
    """, {"topic": topic}, read_only=True)
    return r.rows[0] if r.rows else None


def record_outcome(tenant: str, ticket_id: str, resolved: bool) -> dict | None:
    """Close the loop: the outcome of a ticket updates the procedure that was used."""
    r = db.run(tenant, """
        MATCH (t:Ticket {id:$tid})
        SET t.status = CASE WHEN $ok THEN 'resolved' ELSE 'reopened' END, t.closed = $ts
        WITH t
        OPTIONAL MATCH (t)-[:USED_PROCEDURE]->(p:Procedure)
        FOREACH (_ IN CASE WHEN p IS NULL OR NOT $ok THEN [] ELSE [1] END | SET p.successes = p.successes + 1)
        FOREACH (_ IN CASE WHEN p IS NULL OR $ok THEN [] ELSE [1] END | SET p.failures = p.failures + 1)
        RETURN t.id AS ticket, t.status AS status, p.name AS procedure, p.successes AS successes, p.failures AS failures
    """, {"tid": ticket_id, "ok": resolved, "ts": _now()})
    return r.rows[0] if r.rows else None


# --- shared state / coordination ------------------------------------------

def route(tenant: str, topic: str) -> str:
    r = db.run(tenant, "MATCH (a:Agent)-[:CAN_HANDLE]->(:Topic {name:$t}) RETURN a.name AS agent LIMIT 1",
               {"t": topic}, read_only=True)
    return r.rows[0]["agent"] if r.rows else "triage"


def open_or_reuse_ticket(tenant: str, customer_id: str, session_id: str, topic: str, summary: str,
                         about: list[str]) -> dict:
    """Reuse the customer's open ticket on this topic (continuity across sessions) or open a new one."""
    r = db.run(tenant, """
        MATCH (c:Customer {id:$cid})-[:OPENED]->(t:Ticket {topic:$topic})
        WHERE t.status IN ['open', 'reopened']
        RETURN t.id AS id, t.summary AS summary ORDER BY t.created DESC LIMIT 1
    """, {"cid": customer_id, "topic": topic}, read_only=True)
    if r.rows:
        tid, reused = r.rows[0]["id"], True
        db.run(tenant, "MATCH (t:Ticket {id:$tid}), (s:Session {id:$sid}) MERGE (t)-[:IN_SESSION]->(s)",
               {"tid": tid, "sid": session_id})
    else:
        tid, reused = _id("TK"), False
        db.run(tenant, """
            MATCH (c:Customer {id:$cid}), (s:Session {id:$sid})
            CREATE (c)-[:OPENED]->(t:Ticket {id:$tid, topic:$topic, status:'open', summary:$summary, created:$ts})-[:IN_SESSION]->(s)
        """, {"cid": customer_id, "sid": session_id, "tid": tid, "topic": topic, "summary": summary, "ts": _now()})
    db.run(tenant, """
        MATCH (t:Ticket {id:$tid}) UNWIND $about AS a
        MATCH (x) WHERE (x:Feature AND x.name = a) OR (x:Invoice AND x.id = a)
        MERGE (t)-[:ABOUT]->(x)
    """, {"tid": tid, "about": about})
    return {"id": tid, "reused": reused}


def claim_ticket(tenant: str, ticket_id: str, agent: str) -> str:
    """Atomic claim. FalkorDB runs each write query atomically per graph, so if many agents
    race for the same ticket exactly one of them wins; everyone else reads the winner."""
    r = db.run(tenant, """
        MATCH (t:Ticket {id:$tid})
        SET t.owner = coalesce(t.owner, $agent)
        WITH t
        MATCH (a:Agent {name:t.owner})
        MERGE (t)-[:ASSIGNED_TO]->(a)
        RETURN t.owner AS owner
    """, {"tid": ticket_id, "agent": agent})
    return r.rows[0]["owner"]


def reassign(tenant: str, ticket_id: str, agent: str) -> None:
    db.run(tenant, """
        MATCH (t:Ticket {id:$tid}), (a:Agent {name:$agent})
        OPTIONAL MATCH (t)-[old:ASSIGNED_TO]->()
        DELETE old
        SET t.owner = $agent
        MERGE (t)-[:ASSIGNED_TO]->(a)
    """, {"tid": ticket_id, "agent": agent})


def handoff(tenant: str, ticket_id: str, from_agent: str, to_agent: str, reason: str) -> dict:
    """Record a handoff and attach the customer's memories to it, so the receiving agent
    starts with the context instead of asking the customer again."""
    hid = _id("H")
    r = db.run(tenant, """
        MATCH (t:Ticket {id:$tid})<-[:OPENED]-(c:Customer), (f:Agent {name:$from}), (to:Agent {name:$to})
        CREATE (h:Handoff {id:$hid, reason:$reason, ts:$ts})
        CREATE (h)-[:FROM]->(f), (h)-[:TO]->(to), (h)-[:FOR]->(t)
        WITH h, c
        OPTIONAL MATCH (c)-[:HAS_MEMORY]->(m:Memory)
        FOREACH (x IN CASE WHEN m IS NULL THEN [] ELSE [m] END | MERGE (h)-[:CARRIES]->(x))
        RETURN h.id AS id, count(m) AS carried
    """, {"tid": ticket_id, "from": from_agent, "to": to_agent, "hid": hid, "reason": reason, "ts": _now()})
    reassign(tenant, ticket_id, to_agent)
    return r.rows[0] if r.rows else {"id": hid, "carried": 0}


def handoff_context(tenant: str, ticket_id: str) -> db.QueryResult:
    """What the receiving agent reads: the memories carried by every handoff on this ticket."""
    return db.run(tenant, """
        MATCH p=(f:Agent)<-[:FROM]-(h:Handoff)-[:FOR]->(t:Ticket {id:$tid})
        OPTIONAL MATCH (h)-[:CARRIES]->(m:Memory)
        RETURN h.id AS handoff, h.ts AS at, f.name AS from_agent, h.reason AS reason, collect(m.text) AS carried, p
        ORDER BY at DESC LIMIT 3
    """, {"tid": ticket_id}, read_only=True)


def use_procedure(tenant: str, ticket_id: str, procedure: str) -> None:
    db.run(tenant, """
        MATCH (t:Ticket {id:$tid}), (p:Procedure {name:$p})
        OPTIONAL MATCH (t)-[old:USED_PROCEDURE]->()
        DELETE old
        MERGE (t)-[:USED_PROCEDURE]->(p)
    """, {"tid": ticket_id, "p": procedure})


def entity_names(tenant: str) -> dict[str, list[str]]:
    """Vocabulary for entity linking: names of things that exist in this tenant's graph."""
    r = db.run(tenant, """
        MATCH (f:Feature) WITH collect(f.name) AS features
        MATCH (i:Invoice) WITH features, collect(i.id) AS invoices
        MATCH (n:Incident) RETURN features, invoices, collect(n.id) AS incidents
    """, read_only=True)
    return r.rows[0] if r.rows else {"features": [], "invoices": [], "incidents": []}
