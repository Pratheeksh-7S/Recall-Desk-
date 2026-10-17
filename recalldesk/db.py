"""FalkorDB connection and per-tenant graph selection.

Every company (tenant) gets its own isolated graph inside one FalkorDB
instance: `recalldesk:<tenant>`. Agents never filter by tenant in Cypher;
isolation comes from FalkorDB's multigraph support.
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

from falkordb import FalkorDB, Graph
from falkordb.edge import Edge
from falkordb.node import Node
from falkordb.path import Path

GRAPH_PREFIX = "recalldesk:"
_TENANT_RE = re.compile(r"^[a-z0-9_-]{1,40}$")

_db: FalkorDB | None = None


def connect() -> FalkorDB:
    global _db
    if _db is None:
        url = os.getenv("FALKORDB_URL")
        if url:
            _db = FalkorDB.from_url(url)
        else:
            _db = FalkorDB(
                host=os.getenv("FALKORDB_HOST", "localhost"),
                port=int(os.getenv("FALKORDB_PORT", "6379")),
                username=os.getenv("FALKORDB_USERNAME") or None,
                password=os.getenv("FALKORDB_PASSWORD") or None,
                ssl=os.getenv("FALKORDB_TLS", "").lower() in ("1", "true", "yes"),
            )
    return _db


def graph_for(tenant: str) -> Graph:
    if not _TENANT_RE.match(tenant):
        raise ValueError(f"invalid tenant id: {tenant!r}")
    return connect().select_graph(GRAPH_PREFIX + tenant)


def list_tenants() -> list[str]:
    return sorted(g[len(GRAPH_PREFIX):] for g in connect().list_graphs() if g.startswith(GRAPH_PREFIX))


@dataclass
class QueryResult:
    """Rows plus everything the UI needs to draw the subgraph behind them."""

    cypher: str
    params: dict[str, Any]
    columns: list[str]
    rows: list[dict[str, Any]]
    ms: float
    nodes: dict[int, dict] = field(default_factory=dict)
    edges: dict[int, dict] = field(default_factory=dict)


def _node_view(n: Node) -> dict:
    props = dict(n.properties)
    label = n.labels[0] if n.labels else "Node"
    if label == "Memory":
        caption = props.get("text", "")
    elif label == "Session":
        caption = "session " + str(props.get("started", ""))[5:16].replace("T", " ")
    elif label == "Handoff":
        caption = "handoff: " + str(props.get("reason", ""))
    elif label == "Turn":
        caption = f"{props.get('agent')}: {props.get('text', '')}"
    else:
        caption = props.get("name") or props.get("title") or props.get("id") or ""
    caption = str(caption)
    return {"id": n.id, "label": label, "caption": str(caption), "props": props}


def _plain(value: Any, res: QueryResult) -> Any:
    """Convert FalkorDB objects to JSON-safe values and collect graph elements."""
    if isinstance(value, Node):
        res.nodes[value.id] = _node_view(value)
        return {"_node": value.id, **value.properties}
    if isinstance(value, Edge):
        res.edges[value.id] = {
            "id": value.id,
            "type": value.relation,
            "src": value.src_node,
            "dst": value.dest_node,
        }
        return {"_edge": value.relation}
    if isinstance(value, Path):
        for n in value.nodes():
            res.nodes[n.id] = _node_view(n)
        for e in value.edges():
            res.edges[e.id] = {"id": e.id, "type": e.relation, "src": e.src_node, "dst": e.dest_node}
        return [n.properties.get("name") or n.properties.get("id") or n.labels[0] for n in value.nodes()]
    if isinstance(value, list):
        return [_plain(v, res) for v in value]
    if isinstance(value, dict):
        return {k: _plain(v, res) for k, v in value.items()}
    return value


def run(tenant: str, cypher: str, params: dict[str, Any] | None = None, read_only: bool = False) -> QueryResult:
    g = graph_for(tenant)
    params = params or {}
    t0 = time.perf_counter()
    raw = g.ro_query(cypher, params) if read_only else g.query(cypher, params)
    ms = (time.perf_counter() - t0) * 1000
    columns = [h[1] if isinstance(h, (list, tuple)) else h for h in (raw.header or [])]
    columns = [c.decode() if isinstance(c, bytes) else str(c) for c in columns]
    res = QueryResult(cypher=cypher.strip(), params=params, columns=columns, rows=[], ms=round(ms, 2))
    for row in raw.result_set or []:
        res.rows.append({c: _plain(v, res) for c, v in zip(columns, row)})
    return res
