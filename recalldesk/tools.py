"""The fixed catalogue of graph tools the agents can call.

The LLM picks a tool and fills its arguments; it never writes Cypher. Every
query here is parameterised, read-only unless marked, and returns paths where
it can so the UI can show how the answer was reached.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import db


@dataclass(frozen=True)
class Tool:
    name: str
    agents: tuple[str, ...]
    description: str
    params: tuple[str, ...]
    cypher: str


TOOLS: dict[str, Tool] = {}


def _tool(name, agents, description, params, cypher):
    TOOLS[name] = Tool(name, tuple(agents), description, tuple(params), cypher)


# --- shared --------------------------------------------------------------

_tool(
    "customer_profile", ["triage", "billing", "technical"],
    "Who the customer is: plan, region, features their plan includes.",
    ["customer_id"],
    """
    MATCH p=(c:Customer {id:$customer_id})-[:SUBSCRIBES_TO]->(pl:Plan)-[:INCLUDES]->(f:Feature)
    MATCH (c)-[:IN_REGION]->(r:Region)
    RETURN c.name AS customer, pl.name AS plan, r.name AS region, collect(f.name) AS features, collect(p) AS paths
    """,
)

# --- billing ---------------------------------------------------------------

_tool(
    "billing_overview", ["billing"],
    "All invoices and payment methods for the customer.",
    ["customer_id"],
    """
    MATCH p=(c:Customer {id:$customer_id})-[:BILLED]->(i:Invoice)
    OPTIONAL MATCH pm=(c)-[:PAYS_WITH]->(m:PaymentMethod)
    RETURN i.id AS invoice, i.amount AS amount, i.status AS status, i.period AS period,
           m.last4 AS card_last4, m.expired AS card_expired, p, pm
    ORDER BY i.period, i.id
    """,
)

_tool(
    "find_duplicate_charges", ["billing"],
    "Pairs of paid invoices for the same customer, same period and same amount (a double charge).",
    ["customer_id"],
    """
    MATCH p1=(c:Customer {id:$customer_id})-[:BILLED]->(a:Invoice {status:'paid'}),
          p2=(c)-[:BILLED]->(b:Invoice {status:'paid'})
    WHERE a.id < b.id AND a.period = b.period AND a.amount = b.amount AND a.kind = b.kind
    RETURN a.id AS first_invoice, b.id AS duplicate_invoice, a.amount AS amount, a.period AS period, p1, p2
    """,
)

_tool(
    "failed_payments", ["billing"],
    "Failed or overdue invoices and whether the card on file has expired.",
    ["customer_id"],
    """
    MATCH p=(c:Customer {id:$customer_id})-[:BILLED]->(i:Invoice)
    WHERE i.status IN ['failed', 'overdue']
    OPTIONAL MATCH pm=(c)-[:PAYS_WITH]->(m:PaymentMethod)
    RETURN i.id AS invoice, i.status AS status, i.amount AS amount, m.last4 AS card_last4, m.expired AS card_expired, p, pm
    """,
)

# --- technical (multi-hop) -------------------------------------------------

_tool(
    "trace_feature_outage", ["technical"],
    "Multi-hop: follow the customer's plan to a feature, through every service it transitively depends on, "
    "to any OPEN incident in the customer's region. Returns the full dependency path.",
    ["customer_id", "feature"],
    """
    MATCH (c:Customer {id:$customer_id})-[:IN_REGION]->(r:Region)
    MATCH p=(c)-[:SUBSCRIBES_TO]->(:Plan)-[:INCLUDES]->(f:Feature)-[:DEPENDS_ON*1..6]->(s:Service)<-[:ON]-(i:Incident {status:'open'})-[:IN_REGION]->(r)
    WHERE toLower(f.name) = toLower($feature)
    RETURN f.name AS feature, s.name AS root_service, i.id AS incident, i.title AS incident_title,
           i.started AS started, length(p) AS hops, p
    ORDER BY hops
    LIMIT 3
    """,
)

_tool(
    "feature_dependencies", ["technical"],
    "Every service a feature depends on, directly or transitively, with hop depth.",
    ["feature"],
    """
    MATCH p=(f:Feature)-[:DEPENDS_ON*1..6]->(s:Service)
    WHERE toLower(f.name) = toLower($feature)
    RETURN s.name AS service, length(p) AS depth, p
    ORDER BY depth
    """,
)

_tool(
    "incident_blast_radius", ["technical"],
    "Reverse traversal: which customers in the incident's region are affected through any feature that depends on the broken service.",
    ["incident_id"],
    """
    MATCH (i:Incident {id:$incident_id})-[:ON]->(s:Service), (i)-[:IN_REGION]->(r:Region)
    MATCH p=(c:Customer)-[:SUBSCRIBES_TO]->(:Plan)-[:INCLUDES]->(f:Feature)-[:DEPENDS_ON*1..6]->(s)
    WHERE (c)-[:IN_REGION]->(r)
    RETURN c.name AS customer, collect(DISTINCT f.name) AS via_features, count(DISTINCT p) AS paths
    ORDER BY customer
    """,
)

_tool(
    "critical_services", ["technical"],
    "PageRank over the service dependency graph: the services most other things rely on.",
    [],
    """
    CALL algo.pageRank('Service', 'DEPENDS_ON') YIELD node, score
    RETURN node.name AS service, round(score * 1000) / 1000 AS score
    ORDER BY score DESC LIMIT 5
    """,
)

_tool(
    "related_tickets", ["technical", "triage"],
    "Other customers' tickets whose feature shares a service dependency with this feature (the same root cause, seen by others).",
    ["customer_id", "feature"],
    """
    MATCH (f:Feature)-[:DEPENDS_ON*1..6]->(s:Service)
    WHERE toLower(f.name) = toLower($feature)
    MATCH p=(t:Ticket)-[:ABOUT]->(of:Feature)-[:DEPENDS_ON*1..6]->(s)
    MATCH (other:Customer)-[:OPENED]->(t)
    WHERE other.id <> $customer_id
    RETURN DISTINCT t.id AS ticket, other.name AS customer, of.name AS feature, s.name AS shared_service, t.status AS status
    LIMIT 10
    """,
)

_tool(
    "kb_articles", ["technical", "billing"],
    "Help-centre articles about a feature or topic.",
    ["about"],
    """
    MATCH p=(a:Article)-[:ABOUT]->(x)
    WHERE toLower(x.name) = toLower($about)
    RETURN a.id AS article, a.title AS title, a.body AS body, p
    """,
)


def for_agent(agent: str) -> list[Tool]:
    return [t for t in TOOLS.values() if agent in t.agents]


def call(tenant: str, name: str, args: dict) -> db.QueryResult:
    tool = TOOLS[name]
    params = {k: args.get(k) for k in tool.params}
    missing = [k for k, v in params.items() if v in (None, "")]
    if missing:
        raise ValueError(f"{name} needs {', '.join(missing)}")
    return db.run(tenant, tool.cypher, params, read_only=True)
