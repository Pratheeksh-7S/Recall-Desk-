"""Synthetic company data. Every name, email and number here is made up.

Run:  python -m recalldesk.seed            (resets and seeds both tenants)
"""
from __future__ import annotations

import sys

from . import db

INDEXES = [
    "CREATE INDEX FOR (c:Customer) ON (c.id)",
    "CREATE INDEX FOR (f:Feature) ON (f.name)",
    "CREATE INDEX FOR (s:Service) ON (s.name)",
    "CREATE INDEX FOR (t:Ticket) ON (t.id)",
    "CREATE INDEX FOR (s:Session) ON (s.id)",
    "CREATE INDEX FOR (m:Memory) ON (m.id)",
    "CREATE INDEX FOR (a:Agent) ON (a.name)",
    "CREATE INDEX FOR (i:Invoice) ON (i.id)",
    "CREATE INDEX FOR (t:Topic) ON (t.name)",
]

AGENTS = [
    # name, role, topics it can handle
    ("triage", "Greets customers, reads memory, routes to the right specialist", ["general", "account"]),
    ("billing", "Invoices, charges, refunds, plans", ["billing"]),
    ("technical", "Outages, bugs, feature problems", ["technical"]),
]

TENANTS = {
    "acme": {
        "company": "Acme Cloud Drive",
        "regions": ["eu-west", "us-east", "ap-south"],
        "plans": [("Free", 0, ["File Sync"]), ("Pro", 12, ["File Sync", "Sharing Links"]),
                  ("Team", 30, ["File Sync", "Sharing Links", "Bulk Export", "Audit Log"]),
                  ("Enterprise", 90, ["File Sync", "Sharing Links", "Bulk Export", "Audit Log", "SSO", "API Access"])],
        # feature -> first service it depends on
        "feature_deps": {"File Sync": "sync-engine", "Sharing Links": "link-service", "Bulk Export": "export-worker",
                         "Audit Log": "log-pipeline", "SSO": "auth-gateway", "API Access": "api-gateway"},
        # service -> services it depends on
        "service_deps": {"sync-engine": ["object-store"], "link-service": ["object-store", "auth-gateway"],
                         "export-worker": ["job-queue"], "job-queue": ["object-store"], "log-pipeline": ["job-queue"],
                         "api-gateway": ["auth-gateway"], "auth-gateway": [], "object-store": []},
        "incidents": [("INC-301", "Elevated latency on object-store", "object-store", "eu-west", "open", "2026-10-17T06:10"),
                      ("INC-288", "auth-gateway token refresh errors", "auth-gateway", "us-east", "resolved", "2026-10-09T13:00")],
        "customers": [
            ("C-1001", "Priya Raman", "priya@example.com", "Team", "eu-west"),
            ("C-1002", "Marco Bellini", "marco@example.com", "Pro", "us-east"),
            ("C-1003", "Aiko Tanaka", "aiko@example.com", "Enterprise", "ap-south"),
            ("C-1004", "Lena Fischer", "lena@example.com", "Team", "eu-west"),
            ("C-1005", "Omar Haddad", "omar@example.com", "Free", "eu-west"),
            ("C-1006", "Grace Okafor", "grace@example.com", "Enterprise", "eu-west"),
        ],
        # id, customer, amount, status, period, kind
        "invoices": [
            ("INV-5001", "C-1001", 30, "paid", "2026-09", "subscription"),
            ("INV-5002", "C-1001", 30, "paid", "2026-10", "subscription"),
            ("INV-5003", "C-1001", 30, "paid", "2026-10", "subscription"),  # duplicate charge
            ("INV-5004", "C-1002", 12, "failed", "2026-10", "subscription"),
            ("INV-5005", "C-1003", 90, "paid", "2026-10", "subscription"),
            ("INV-5006", "C-1004", 30, "paid", "2026-10", "subscription"),
            ("INV-5007", "C-1006", 90, "overdue", "2026-10", "subscription"),
        ],
        "payment_methods": [("C-1001", "4242", False), ("C-1002", "0005", True), ("C-1003", "1881", False),
                            ("C-1004", "3333", False), ("C-1006", "7777", False)],
        "articles": [
            ("KB-11", "Exports stuck in 'queued'", "Bulk Export", "Exports queue behind object-store writes. If an object-store incident is open, exports resume automatically once it clears; no need to restart."),
            ("KB-12", "Sync shows 'waiting for server'", "File Sync", "Usually caused by object-store latency. Pause and resume sync after the incident clears."),
            ("KB-21", "Setting up SAML SSO", "SSO", "Upload your IdP metadata under Admin > Security."),
            ("KB-31", "Refund policy", "billing", "Duplicate charges are refunded in full within 5 business days. Plan downgrades are prorated."),
            ("KB-32", "Updating an expired card", "billing", "Go to Billing > Payment method and add a new card; failed invoices retry within 24 hours."),
        ],
        # procedural memory: name, topic, steps, successes, failures
        # tickets other customers already opened today: id, customer, topic, feature, summary
        "tickets": [("TK-0001", "C-1004", "technical", "Bulk Export", "Export job never finishes"),
                    ("TK-0002", "C-1006", "technical", "File Sync", "Sync says waiting for server")],
        "procedures": [
            ("Refund duplicate charge", "billing", ["Confirm both invoices are for the same period", "Refund the newer invoice", "Tell the customer the refund takes 5 business days"], 7, 1),
            ("Recover failed payment", "billing", ["Check the payment method on file", "If the card is expired ask for a new one", "Retry the invoice"], 5, 2),
            ("Offer plan credit", "billing", ["Apologise", "Offer one month of credit"], 1, 4),
            ("Diagnose feature outage", "technical", ["Trace the feature's service dependencies", "Check for open incidents in the customer's region", "Link the ticket to the incident and share the status page"], 9, 1),
            ("Restart client", "technical", ["Ask the customer to sign out and in", "Clear the local cache"], 2, 6),
        ],
    },
    "globex": {
        "company": "Globex Telecom",
        "regions": ["north", "south"],
        "plans": [("Basic", 15, ["Voice"]), ("Plus", 35, ["Voice", "Mobile Data"]), ("Max", 60, ["Voice", "Mobile Data", "Roaming"])],
        "feature_deps": {"Voice": "voice-core", "Mobile Data": "packet-core", "Roaming": "roaming-hub"},
        "service_deps": {"voice-core": ["cell-network"], "packet-core": ["cell-network"], "roaming-hub": ["partner-link"],
                         "cell-network": [], "partner-link": []},
        "incidents": [("INC-77", "Cell tower outage", "cell-network", "south", "open", "2026-10-17T03:00")],
        "customers": [
            ("C-2001", "Priya Raman", "priya@example.com", "Plus", "south"),   # same person, other tenant: must not leak
            ("C-2002", "Diego Alvarez", "diego@example.com", "Max", "north"),
        ],
        "invoices": [("INV-9001", "C-2001", 35, "paid", "2026-10", "subscription"),
                     ("INV-9002", "C-2002", 60, "paid", "2026-10", "subscription")],
        "payment_methods": [("C-2001", "1111", False), ("C-2002", "2222", False)],
        "articles": [("KB-1", "No signal", "Mobile Data", "Check the outage map; service returns automatically.")],
        "procedures": [("Diagnose feature outage", "technical", ["Trace dependencies", "Check incidents in region"], 3, 0),
                       ("Refund duplicate charge", "billing", ["Refund newer invoice"], 2, 0)],
    },
}


def seed_tenant(tenant: str, spec: dict) -> None:
    g = db.graph_for(tenant)
    try:
        g.delete()
    except Exception:
        pass
    q = lambda cypher, **p: db.run(tenant, cypher, p)  # noqa: E731
    for idx in INDEXES:
        try:
            q(idx)
        except Exception:
            pass

    q("CREATE (:Company {name:$name})", name=spec["company"])
    for name, role, topics in AGENTS:
        q("""MERGE (a:Agent {name:$name}) SET a.role=$role
             WITH a UNWIND $topics AS t MERGE (tp:Topic {name:t}) MERGE (a)-[:CAN_HANDLE]->(tp)""",
          name=name, role=role, topics=topics)
    q("UNWIND $r AS r CREATE (:Region {name:r})", r=spec["regions"])
    for plan, price, features in spec["plans"]:
        q("""CREATE (p:Plan {name:$plan, price:$price})
             WITH p UNWIND $features AS f MERGE (ft:Feature {name:f}) MERGE (p)-[:INCLUDES]->(ft)""",
          plan=plan, price=price, features=features)
    for svc, deps in spec["service_deps"].items():
        q("MERGE (s:Service {name:$s}) WITH s UNWIND $deps AS d MERGE (x:Service {name:d}) MERGE (s)-[:DEPENDS_ON]->(x)",
          s=svc, deps=deps)
    for feat, svc in spec["feature_deps"].items():
        q("MATCH (f:Feature {name:$f}), (s:Service {name:$s}) MERGE (f)-[:DEPENDS_ON]->(s)", f=feat, s=svc)
    for iid, title, svc, region, status, ts in spec["incidents"]:
        q("""MATCH (s:Service {name:$svc}), (r:Region {name:$region})
             CREATE (i:Incident {id:$id, title:$title, status:$status, started:$ts})-[:ON]->(s), (i)-[:IN_REGION]->(r)""",
          id=iid, title=title, svc=svc, region=region, status=status, ts=ts)
    for cid, name, email, plan, region in spec["customers"]:
        q("""MATCH (p:Plan {name:$plan}), (r:Region {name:$region})
             CREATE (c:Customer {id:$id, name:$name, email:$email})-[:SUBSCRIBES_TO]->(p), (c)-[:IN_REGION]->(r)""",
          id=cid, name=name, email=email, plan=plan, region=region)
    for iid, cid, amount, status, period, kind in spec["invoices"]:
        q("""MATCH (c:Customer {id:$cid})
             CREATE (c)-[:BILLED]->(:Invoice {id:$id, amount:$amount, status:$status, period:$period, kind:$kind})""",
          id=iid, cid=cid, amount=amount, status=status, period=period, kind=kind)
    for cid, last4, expired in spec["payment_methods"]:
        q("MATCH (c:Customer {id:$cid}) CREATE (c)-[:PAYS_WITH]->(:PaymentMethod {last4:$l, expired:$e})",
          cid=cid, l=last4, e=expired)
    for aid, title, about, body in spec["articles"]:
        q("""OPTIONAL MATCH (f:Feature {name:$about}) OPTIONAL MATCH (t:Topic {name:$about})
             WITH coalesce(f, t) AS target
             CREATE (a:Article {id:$id, title:$title, body:$body})
             FOREACH (_ IN CASE WHEN target IS NULL THEN [] ELSE [1] END | CREATE (a)-[:ABOUT]->(target))""",
          id=aid, title=title, about=about, body=body)
    for name, topic, steps, ok, bad in spec["procedures"]:
        q("""MATCH (t:Topic {name:$topic})
             CREATE (p:Procedure {name:$name, steps:$steps, successes:$ok, failures:$bad})-[:FOR_TOPIC]->(t)""",
          name=name, topic=topic, steps=steps, ok=ok, bad=bad)
    _seed_tickets(tenant, spec)


def _seed_tickets(tenant: str, spec: dict) -> None:
    for tid, cid, topic, feature, summary in spec.get("tickets", []):
        db.run(tenant, """
            MATCH (c:Customer {id:$cid}), (f:Feature {name:$feature}), (a:Agent {name:$topic})
            CREATE (c)-[:OPENED]->(t:Ticket {id:$tid, topic:$topic, status:'open', summary:$summary,
                                             created:'2026-10-17T07:00:00+00:00', owner:$topic})-[:ABOUT]->(f),
                   (t)-[:ASSIGNED_TO]->(a)
        """, {"cid": cid, "feature": feature, "topic": topic, "tid": tid, "summary": summary})


def seed_all() -> None:
    for tenant, spec in TENANTS.items():
        seed_tenant(tenant, spec)
        print(f"seeded tenant {tenant!r} ({spec['company']})")


if __name__ == "__main__":
    only = sys.argv[1:]
    for t, s in TENANTS.items():
        if not only or t in only:
            seed_tenant(t, s)
            print(f"seeded tenant {t!r} ({s['company']})")
