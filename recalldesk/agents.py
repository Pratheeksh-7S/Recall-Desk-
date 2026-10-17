"""The support desk: a triage agent and two specialists sharing one memory graph.

Flow for every customer message:
  1. triage   reads the customer's memory from the graph (all past sessions, all agents)
  2. triage   works out topic / entities / facts worth remembering, writes them as Memory nodes
  3. triage   opens or reuses a Ticket, asks the graph which agent CAN_HANDLE the topic, hands off
  4. expert   reads the handoff context, picks graph tools, runs them (multi-hop Cypher, PageRank)
  5. expert   picks the procedure with the best outcome record, acts, writes what it did to memory
  6. expert   answers only from graph rows; the answer is path-checked before it is sent
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import date, timedelta

from . import db, llm, memory, tools

BILLING_WORDS = r"charg|invoice|refund|bill|payment|card|paid|twice|double|price|money|subscription|overdue|declin"
TECH_WORDS = r"error|not working|stuck|slow|down|fail|broken|outage|crash|can't|cannot|won't|timeout|queued|loading|signal"
PATH_COLUMNS = {"p", "pm", "p1", "p2", "paths", "mpaths", "tpaths"}


@dataclass
class Trace:
    steps: list[dict] = field(default_factory=list)
    nodes: dict[int, dict] = field(default_factory=dict)
    edges: dict[int, dict] = field(default_factory=dict)
    evidence: set[str] = field(default_factory=set)

    def add(self, agent: str, action: str, detail: str = "", result: db.QueryResult | None = None,
            kind: str = "read", tool: str | None = None) -> None:
        step = {"agent": agent, "action": action, "detail": detail, "kind": kind, "tool": tool}
        if result is not None:
            step.update(cypher=result.cypher, params=result.params, ms=result.ms,
                        rows=[{k: v for k, v in r.items() if k not in PATH_COLUMNS} for r in result.rows][:8],
                        row_count=len(result.rows))
            self.nodes.update(result.nodes)
            self.edges.update(result.edges)
            for n in result.nodes.values():
                for key in ("id", "name", "title"):
                    if n["props"].get(key):
                        self.evidence.add(str(n["props"][key]).lower())
            for r in step["rows"]:
                _collect_strings(r, self.evidence)
        self.steps.append(step)


def _collect_strings(value, out: set[str]) -> None:
    if isinstance(value, str):
        out.add(value.lower())
    elif isinstance(value, dict):
        for v in value.values():
            _collect_strings(v, out)
    elif isinstance(value, list):
        for v in value:
            _collect_strings(v, out)


# --- 1-2. understanding the message ----------------------------------------

def _match_feature(text: str, features: list[str]) -> str | None:
    low = text.lower()
    for f in features:
        if f.lower() in low:
            return f
    for f in features:
        for word in f.lower().split():
            if len(word) > 3 and re.search(rf"\b{re.escape(word)}", low):
                return f
    return None


def _rule_facts(text: str) -> list[dict]:
    facts = []
    low = text.lower()
    m = re.search(r"prefer (?:to be contacted by |contact by |)(email|phone|sms|chat|whatsapp)", low)
    if m:
        facts.append({"kind": "preference", "text": f"Prefers contact by {m.group(1)}"})
    m = re.search(r"(?:i'm|i am) (?:away|on holiday|travelling|traveling|out of office) until ([\w ]+)", low)
    if m:
        facts.append({"kind": "context", "text": f"Away until {m.group(1).strip()}"})
    m = re.search(r"(?:we use|we rely on|our team uses) ([\w -]+?) for ([\w -]+)", low)
    if m:
        facts.append({"kind": "context", "text": f"Uses {m.group(1).strip()} for {m.group(2).strip()}"})
    if re.search(r"(urgent|asap|deadline|board meeting|client demo)", low):
        facts.append({"kind": "context", "text": "Said this is time-sensitive"})
    return facts


def understand(text: str, vocab: dict, history: list[dict], recalled: dict) -> dict:
    feature = _match_feature(text, vocab["features"])
    invoice = next((i for i in vocab["invoices"] if i.lower() in text.lower()), None)
    if re.search(BILLING_WORDS, text, re.I):
        topic = "billing"
    elif feature or re.search(TECH_WORDS, text, re.I):
        topic = "technical"
    else:
        topic = "general"
    # a follow-up like "any update?" continues the customer's latest open ticket
    if topic == "general" and recalled.get("tickets"):
        open_t = [t for t in recalled["tickets"] if t["status"] in ("open", "reopened")]
        if open_t:
            topic = sorted(open_t, key=lambda t: t["created"] or "")[-1]["topic"]
    base = {"topic": topic, "feature": feature, "invoice": invoice, "facts": _rule_facts(text),
            "summary": text[:120], "planner": "rules"}

    if not llm.available():
        return base
    out = llm.json_call(
        "You are the triage agent of a customer support desk. Classify the customer's message.\n"
        'Return {"topic": "billing"|"technical"|"general", "feature": <exact feature name from the list or null>, '
        '"invoice": <invoice id from the list or null>, "summary": <one line>, '
        '"facts": [{"kind": "preference"|"context", "text": <durable fact about the customer worth remembering '
        'for future conversations, third person>}]}. Only include facts the customer stated; usually none.',
        json.dumps({"message": text, "features": vocab["features"], "invoices": vocab["invoices"],
                    "recent_turns": history[-6:], "open_tickets": recalled.get("tickets", [])}),
        max_tokens=1500,
    )
    if not out:
        return base
    result = dict(base, planner=f"{llm.provider()}:{llm.model_name()}")
    if out.get("topic") in ("billing", "technical", "general"):
        result["topic"] = out["topic"]
    if out.get("feature") in vocab["features"]:
        result["feature"] = out["feature"]
    if out.get("invoice") in vocab["invoices"]:
        result["invoice"] = out["invoice"]
    if isinstance(out.get("summary"), str):
        result["summary"] = out["summary"][:160]
    if isinstance(out.get("facts"), list):
        result["facts"] = [f for f in out["facts"] if isinstance(f, dict) and f.get("text")][:3]
    return result


# --- 4. tool planning --------------------------------------------------------

def plan_tools(agent: str, text: str, u: dict, customer_id: str) -> list[tuple[str, dict]]:
    plan: list[tuple[str, dict]] = []
    if agent == "billing":
        plan.append(("billing_overview", {"customer_id": customer_id}))
        if re.search(r"twice|double|duplicate|two charges|charged again|refund", text, re.I):
            plan.append(("find_duplicate_charges", {"customer_id": customer_id}))
        if re.search(r"fail|declin|card|overdue|expired|didn't go through", text, re.I):
            plan.append(("failed_payments", {"customer_id": customer_id}))
        plan.append(("kb_articles", {"about": "billing"}))
    elif agent == "technical":
        plan.append(("customer_profile", {"customer_id": customer_id}))
        if u.get("feature"):
            plan += [("trace_feature_outage", {"customer_id": customer_id, "feature": u["feature"]}),
                     ("related_tickets", {"customer_id": customer_id, "feature": u["feature"]}),
                     ("kb_articles", {"about": u["feature"]})]
        else:
            plan.append(("critical_services", {}))
    else:
        plan.append(("customer_profile", {"customer_id": customer_id}))

    if not llm.available() or agent == "triage":
        return plan
    catalogue = [{"name": t.name, "description": t.description, "params": list(t.params)} for t in tools.for_agent(agent)]
    out = llm.json_call(
        f"You are the {agent} agent of a support desk. Pick the graph tools to run for this message, in order. "
        'Return {"calls": [{"tool": <name>, "args": {<param>: <value>}}]}. Use at most 4 calls. '
        "customer_id is always the given one. Feature names must come from the context.",
        json.dumps({"message": text, "customer_id": customer_id, "feature": u.get("feature"),
                    "invoice": u.get("invoice"), "tools": catalogue}),
        max_tokens=1500,
    )
    allowed = {t.name for t in tools.for_agent(agent)}
    calls = []
    for c in (out or {}).get("calls", [])[:4]:
        if isinstance(c, dict) and c.get("tool") in allowed:
            args = dict(c.get("args") or {})
            if "customer_id" in tools.TOOLS[c["tool"]].params:
                args["customer_id"] = customer_id  # never let the model pick a different customer
            calls.append((c["tool"], args))
    return calls or plan


# --- 5-6. specialist work and answer ---------------------------------------

def _rows(results: dict, name: str) -> list[dict]:
    r = results.get(name)
    return [{k: v for k, v in row.items() if k not in PATH_COLUMNS} for row in r.rows] if r else []


def act(tenant: str, agent: str, results: dict, ticket_id: str, customer_id: str, session_id: str,
        trace: Trace, procedure: dict | None) -> list[str]:
    """Agents change the world based on what the graph said, and remember what they did."""
    actions = []
    dup = _rows(results, "find_duplicate_charges")
    if agent == "billing" and dup:
        inv = dup[0]["duplicate_invoice"]
        due = (date.today() + timedelta(days=7)).isoformat()
        r = db.run(tenant, """
            MATCH (i:Invoice {id:$inv}), (t:Ticket {id:$tid})
            SET i.status = 'refund_pending'
            MERGE (t)-[:ABOUT]->(i)
            MERGE (rf:Refund {invoice:$inv}) ON CREATE SET rf.amount = i.amount, rf.status = 'issued', rf.due = $due
            MERGE (rf)-[:FOR_INVOICE]->(i)
            RETURN rf.amount AS amount, rf.due AS due
        """, {"inv": inv, "tid": ticket_id, "due": due})
        trace.add(agent, "issue refund", f"refund for {inv}", r, kind="write")
        amount = r.rows[0]["amount"]
        text = f"Billing issued a ${amount} refund for duplicate invoice {inv}; due by {r.rows[0]['due']}"
        memory.remember(tenant, customer_id, session_id, agent, "promise", text, about=inv)
        actions.append(text)
    failed = _rows(results, "failed_payments")
    if agent == "billing" and failed and failed[0].get("card_expired"):
        text = f"Invoice {failed[0]['invoice']} failed because card ending {failed[0]['card_last4']} expired; asked for a new card"
        memory.remember(tenant, customer_id, session_id, agent, "fact", text, about=failed[0]["invoice"])
        actions.append(text)
    outage = _rows(results, "trace_feature_outage")
    if agent == "technical" and outage:
        o = outage[0]
        r = db.run(tenant, """
            MATCH (t:Ticket {id:$tid}), (i:Incident {id:$inc})
            MERGE (t)-[:CAUSED_BY]->(i)
            RETURN i.id AS incident
        """, {"tid": ticket_id, "inc": o["incident"]})
        trace.add(agent, "link ticket to incident", o["incident"], r, kind="write")
        text = f"{o['feature']} problem traced to {o['incident']} on {o['root_service']} ({o['hops']} hops)"
        memory.remember(tenant, customer_id, session_id, agent, "fact", text, about=o["incident"])
        actions.append(text)
    if procedure:
        memory.use_procedure(tenant, ticket_id, procedure["name"])
    return actions


def _refund_due(actions: list[str]) -> str | None:
    for a in actions:
        m = re.search(r"due by ([\d-]+)", a)
        if m:
            return m.group(1)
    return None


def _template_answer(agent: str, name: str, results: dict, recalled: dict, actions: list[str],
                     procedure: dict | None, u: dict) -> str:
    first = name.split()[0]
    lines = []
    if agent == "billing":
        dup = _rows(results, "find_duplicate_charges")
        failed = _rows(results, "failed_payments")
        promises = [m for m in recalled.get("memories", []) if m["kind"] == "promise"]
        if dup:
            d = dup[0]
            lines.append(f"Hi {first}, you're right: invoices {d['first_invoice']} and {d['duplicate_invoice']} "
                         f"are both ${d['amount']} for {d['period']}, so I've refunded {d['duplicate_invoice']}"
                         + (f"; the money should be back by {r_due}." if (r_due := _refund_due(actions)) else "."))
        elif promises:
            lines.append(f"Hi {first}, I can see what we promised last time: {promises[0]['text']}.")
        if failed:
            f = failed[0]
            reason = f"the card ending {f['card_last4']} has expired" if f.get("card_expired") else "the payment was declined"
            lines.append(f"Invoice {f['invoice']} (${f['amount']}) is {f['status']} because {reason}. "
                         "Add a new card under Billing > Payment method and it retries within 24 hours.")
        if not lines:
            inv = _rows(results, "billing_overview")
            latest = inv[-1] if inv else None
            lines.append(f"Hi {first}, your billing looks in order"
                         + (f": latest invoice {latest['invoice']} is {latest['status']} (${latest['amount']})." if latest else "."))
    elif agent == "technical":
        outage = _rows(results, "trace_feature_outage")
        blast = _rows(results, "incident_blast_radius")
        related = _rows(results, "related_tickets")
        kb = _rows(results, "kb_articles")
        if outage:
            o = outage[0]
            lines.append(f"Hi {first}, I traced this through our systems: {o['feature']} depends on "
                         f"{o['root_service']}, which has an open incident in your region ({o['incident']}: "
                         f"{o['incident_title']}, since {o['started']}).")
            if blast:
                lines.append(f"It affects {len(blast)} customers in your region, so it's not something on your side.")
            if related:
                lines.append(f"{len(related)} other customer(s) reported the same root cause today.")
            if kb:
                lines.append(f"{kb[0]['title']} ({kb[0]['article']}): {kb[0]['body']}")
        elif u.get("feature"):
            deps = _rows(results, "feature_dependencies")
            lines.append(f"Hi {first}, I checked every service behind {u['feature']}"
                         + (f" ({', '.join(d['service'] for d in deps)})" if deps else "")
                         + " and none has an open incident in your region, so this looks specific to your account.")
        else:
            lines.append(f"Hi {first}, which feature is giving you trouble? I can trace it through our systems.")
    else:
        prof = _rows(results, "customer_profile")
        p = prof[0] if prof else {}
        open_t = [t for t in recalled.get("tickets", []) if t["status"] in ("open", "reopened")]
        lines.append(f"Hi {first}, you're on the {p.get('plan', '?')} plan in {p.get('region', '?')}.")
        if open_t:
            lines.append(f"You have {len(open_t)} open ticket(s): " + "; ".join(t["summary"] for t in open_t) + ".")
        lines.append("What can I help with: billing or something not working?")
    if procedure and agent != "triage":
        lines.append(f"(Playbook: {procedure['name']}, {int(procedure['success_rate'])}% success so far.)")
    return " ".join(lines)


ID_RE = re.compile(r"\b(?:INV|INC|KB|TK)-[\w]+\b")


def path_check(answer: str, trace: Trace, recalled_text: str) -> bool:
    """Every ID the answer names must appear in graph results or recalled memory."""
    known = trace.evidence | {recalled_text.lower()}
    for ident in ID_RE.findall(answer):
        if not any(ident.lower() in k for k in known):
            return False
    return True


def compose(agent: str, name: str, text: str, results: dict, recalled: dict, actions: list[str],
            procedure: dict | None, u: dict, trace: Trace) -> tuple[str, str]:
    fallback = _template_answer(agent, name, results, recalled, actions, procedure, u)
    if not llm.available():
        return fallback, "rules"
    evidence = {k: _rows(results, k) for k in results}
    out = llm.text(
        f"You are the {agent} agent of a customer support desk, replying to {name}. Use ONLY the facts in the "
        "graph evidence, memories and actions provided; never invent IDs, amounts or dates. Mention the incident, "
        "invoice or article IDs you relied on. Be warm and brief (2-5 sentences), plain text.",
        json.dumps({"message": text, "evidence": evidence, "memories": recalled.get("memories", []),
                    "actions_taken": actions, "procedure": procedure}, default=str),
        max_tokens=2000,
    )
    if out is None:
        return fallback, "rules (LLM unavailable)"
    if path_check(out, trace, json.dumps(recalled, default=str)):
        return out, f"{llm.provider()}:{llm.model_name()}"
    trace.add(agent, "path check failed", "LLM answer named something not in the graph; used graph-built answer")
    return fallback, "rules (path check)"


# --- entry points ----------------------------------------------------------

def _recall_dict(r: db.QueryResult) -> dict:
    row = r.rows[0] if r.rows else {}
    return {"memories": row.get("memories", []), "tickets": row.get("tickets", []), "sessions": row.get("sessions", 0)}


def greet(tenant: str, customer_id: str, session_id: str) -> dict:
    trace = Trace()
    r = memory.recall(tenant, customer_id)
    trace.add("triage", "recall memory", "everything past sessions and agents stored about this customer", r)
    rec = _recall_dict(r)
    name = db.run(tenant, "MATCH (c:Customer {id:$id}) RETURN c.name AS n", {"id": customer_id}, True).rows[0]["n"]
    first = name.split()[0]
    if rec["sessions"] > 1 and (rec["memories"] or rec["tickets"]):
        open_t = [t for t in rec["tickets"] if t["status"] in ("open", "reopened")]
        promises = sorted((m for m in rec["memories"] if m["kind"] in ("promise", "fact")), key=lambda m: m["at"] or "", reverse=True)
        msg = f"Welcome back, {first}."
        promises.sort(key=lambda m: m["kind"] != "promise")  # what we promised matters most
        if promises:
            msg += " Last time: " + "; ".join(m["text"] for m in promises[:2]) + "."
        if open_t:
            msg += f" You have {len(open_t)} open ticket(s)."
        prefs = [m for m in rec["memories"] if m["kind"] == "preference"]
        if prefs:
            msg += f" I'll keep in mind: {prefs[0]['text'].lower()}."
        outcome = "answered from memory"
    else:
        msg = f"Hi {first}, I'm the triage agent. What can I help you with today?"
        outcome = "new conversation"
    memory.add_turn(tenant, session_id, "agent", "triage", msg)
    return _response("triage", msg, outcome, trace, None, rec, "rules")


def _response(agent, answer, outcome, trace: Trace, ticket, recalled, composer, extra=None):
    return {
        "agent": agent, "answer": answer, "outcome": outcome, "ticket": ticket, "composer": composer,
        "memory": recalled, "trace": trace.steps,
        "graph": {"nodes": list(trace.nodes.values()), "edges": list(trace.edges.values())},
        **(extra or {}),
    }


def handle_message(tenant: str, session_id: str, customer_id: str, text: str) -> dict:
    t0 = time.perf_counter()
    trace = Trace()
    vocab = memory.entity_names(tenant)
    history = memory.session_turns(tenant, session_id)

    # 1. triage reads shared memory
    r = memory.recall(tenant, customer_id)
    trace.add("triage", "recall memory", "memories, tickets and sessions from every agent", r)
    recalled = _recall_dict(r)
    name = db.run(tenant, "MATCH (c:Customer {id:$id}) RETURN c.name AS n", {"id": customer_id}, True).rows[0]["n"]

    # 2. understand + remember
    u = understand(text, vocab, history, recalled)
    trace.add("triage", "understand", f"topic={u['topic']} feature={u['feature']} invoice={u['invoice']} via {u['planner']}")
    mentions = [x for x in (u["feature"], u["invoice"]) if x]
    memory.add_turn(tenant, session_id, "customer", "customer", text, mentions)
    for f in u["facts"]:
        memory.remember(tenant, customer_id, session_id, "triage", f.get("kind", "context"), f["text"], u["feature"])
        trace.add("triage", "write memory", f["text"], kind="write")

    if u["topic"] == "general":
        results = {"customer_profile": tools.call(tenant, "customer_profile", {"customer_id": customer_id})}
        trace.add("triage", "graph tool", "customer_profile", results["customer_profile"], tool="customer_profile")
        answer, composer = compose("triage", name, text, results, recalled, [], None, u, trace)
        memory.add_turn(tenant, session_id, "agent", "triage", answer)
        return _response("triage", answer, "answered with graph path", trace, None, recalled, composer,
                         {"ms": round((time.perf_counter() - t0) * 1000)})

    # 3. ticket + routing through the graph + handoff
    ticket = memory.open_or_reuse_ticket(tenant, customer_id, session_id, u["topic"], u["summary"], mentions)
    owner = memory.claim_ticket(tenant, ticket["id"], "triage")
    target = memory.route(tenant, u["topic"])
    trace.add("triage", "route", f"(:Agent)-[:CAN_HANDLE]->(:Topic {{name:'{u['topic']}'}}) -> {target}; "
              f"ticket {ticket['id']} ({'reused from earlier session' if ticket['reused'] else 'new'})", kind="write")
    if owner != target:
        h = memory.handoff(tenant, ticket["id"], owner, target, u["summary"])
        trace.add("triage", "handoff", f"{owner} -> {target}, carrying {h['carried']} memories", kind="write")
    ctx = memory.handoff_context(tenant, ticket["id"])
    trace.add(target, "read handoff", "context the previous agent left on the ticket", ctx)

    # 4. specialist runs graph tools
    results: dict[str, db.QueryResult] = {}
    for name_, args in plan_tools(target, text, u, customer_id):
        try:
            res = tools.call(tenant, name_, args)
        except Exception as e:  # bad args from the planner: record and move on
            trace.add(target, "graph tool failed", f"{name_}: {e}")
            continue
        results[name_] = res
        trace.add(target, "graph tool", tools.TOOLS[name_].description, res, tool=name_)
        if name_ == "trace_feature_outage" and res.rows and "incident_blast_radius" not in results:
            inc = res.rows[0]["incident"]
            br = tools.call(tenant, "incident_blast_radius", {"incident_id": inc})
            results["incident_blast_radius"] = br
            trace.add(target, "graph tool", tools.TOOLS["incident_blast_radius"].description, br, tool="incident_blast_radius")
        if name_ == "trace_feature_outage" and not res.rows and u.get("feature"):
            deps = tools.call(tenant, "feature_dependencies", {"feature": u["feature"]})
            results["feature_dependencies"] = deps
            trace.add(target, "graph tool", tools.TOOLS["feature_dependencies"].description, deps, tool="feature_dependencies")

    # 5. procedural memory + action
    procedure = memory.best_procedure(tenant, u["topic"])
    if procedure:
        trace.add(target, "procedural memory", f"best playbook for {u['topic']}: {procedure['name']} "
                  f"({procedure['successes']} wins / {procedure['failures']} losses)")
    actions = act(tenant, target, results, ticket["id"], customer_id, session_id, trace, procedure)
    for a in actions:
        trace.add(target, "write memory", a, kind="write")

    # 6. answer
    answer, composer = compose(target, name, text, results, recalled, actions, procedure, u, trace)
    memory.add_turn(tenant, session_id, "agent", target, answer, mentions)
    has_path = any(res.rows for res in results.values())
    outcome = "answered with graph path" if has_path else ("answered from memory" if recalled["memories"] else "not in graph")
    return _response(target, answer, outcome, trace, ticket["id"], _recall_dict(memory.recall(tenant, customer_id)),
                     composer, {"ms": round((time.perf_counter() - t0) * 1000), "procedure": procedure,
                                "actions": actions, "understanding": u})
