# Recall Desk

[![tests](https://github.com/Pratheeksh-7S/Recall-Desk-/actions/workflows/tests.yml/badge.svg)](https://github.com/Pratheeksh-7S/Recall-Desk-/actions/workflows/tests.yml)

**A support desk where three AI agents share one memory, and that memory is a FalkorDB graph.**

A triage agent, a billing agent and a technical agent answer customers together. They never pass
context to each other through prompts or a chat log. Everything one agent learns (who the customer
is, what was promised, which incident caused the problem, which playbook worked) is written to the
company's graph, and every other agent reads it from there, in this session or the next one.
Every answer comes with the graph path behind it.

**Hackathon:** Graph Hacks: Context for AI Agents (WeMakeDevs × FalkorDB)
**Track:** 02 · Agent Memory and Coordination (also enters 01 · Agents That Act on Connected Data)

![Graph path behind an answer](docs/graph-path.png)

**Live demo:** https://recall-desk.onrender.com (Render free tier + FalkorDB Cloud; the first load can take about a minute while the server wakes up)

**Blog post:** [Recall Desk on dev.to](https://dev.to/pratheeksh7s/recall-desk-three-ai-support-agents-that-share-one-memory-and-the-memory-is-a-graph-11j1)

**Demo video:** [docs/demo.mp4](https://github.com/Pratheeksh-7S/Recall-Desk-/raw/main/docs/demo.mp4) (2 minutes, captioned; regenerate with `scripts/record_demo.cjs`)

## Why the graph is load-bearing

Take FalkorDB away and nothing works. There is no other store: no SQL tables, no vector DB, and no
conversation history kept in Python.

| What the desk does | How the graph does it |
|---|---|
| "Bulk Export is stuck" gets traced to a root cause | Variable-length traversal `Customer → Plan → Feature → DEPENDS_ON*1..6 → Service ← Incident → Region` (7 hops) finds the open incident **in the customer's region** |
| "Is it just me?" | Reverse traversal from the incident to every customer whose plan reaches the broken service (`incident_blast_radius`) |
| "Others are seeing this" | Tickets that share a transitive service dependency (`related_tickets`) |
| Which service matters most | `algo.pageRank('Service','DEPENDS_ON')` |
| Remembering across sessions | `Session-[:HAS_TURN]->Turn-[:NEXT]->Turn` and `Customer-[:HAS_MEMORY]->Memory`, read at the start of every session |
| Handing a customer to another agent | A `Handoff` node with `CARRIES` edges to the customer's memories; the specialist reads them instead of asking again |
| Picking which agent handles it | `(:Agent)-[:CAN_HANDLE]->(:Topic)`; the routing table is data |
| Learning which playbook works | `Procedure {successes, failures}`; ticket outcomes update the counts and the best playbook can change |
| Many agents at once | Atomic ticket claims (`SET t.owner = coalesce(t.owner, $agent)`) and `MERGE`-based memory writes, tested with 30 racing agents |
| Separate memory per company | One graph per tenant (`recalldesk:acme`, `recalldesk:globex`), with the same customer email in both and nothing leaking |

## The demo story

1. **Session 1.** Priya (Acme Cloud Drive, Team plan, eu-west) says *"I was charged twice this month."*
   Triage reads her memory and opens a ticket. The graph says billing `CAN_HANDLE` billing, so triage
   hands the ticket off. Billing finds two paid $30 invoices for 2026-10, refunds the duplicate (a graph
   write), and stores a `promise` memory.
2. She adds *"Bulk Export has been stuck for hours, urgent."* This becomes a new ticket for the technical
   agent. It follows the 7-hop path to `INC-301` (object-store latency in eu-west), finds 4 affected
   customers and 2 other tickets with the same root cause, links the ticket `CAUSED_BY` the incident,
   and answers with the help-centre article.
3. **Session 2.** Priya comes back. Before she types, triage says *"Welcome back. Last time: Billing
   issued a $30 refund for INV-5003, due by …"*. When she asks *"any update on my refund?"*, the desk
   reuses the same ticket from the earlier session and answers from memory.
4. Marking a ticket *Not resolved* updates the playbook's record. After enough failures the technical
   agent switches playbooks on its own.

## Quick start

You need Docker. An LLM key is optional (see [LLM providers](#llm-providers)).

```bash
git clone https://github.com/Pratheeksh-7S/Recall-Desk-.git
cd Recall-Desk-
docker compose up --build
```

Open **http://localhost:8000** for the desk and **http://localhost:3000** for the FalkorDB Browser
(graph `recalldesk:acme`). Demo data is seeded on first start. Use **Reset demo** to re-seed.

### Without Docker Compose

```bash
docker run -d -p 6379:6379 -p 3000:3000 falkordb/falkordb:latest
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m recalldesk.seed            # optional: the server seeds on first start anyway
uvicorn recalldesk.server:app --port 8000
```

To use FalkorDB Cloud instead, set `FALKORDB_URL=redis://user:password@host:port`.

### Deploy (Render + FalkorDB Cloud)

1. Create a free FalkorDB Cloud instance at [app.falkordb.cloud](https://app.falkordb.cloud) and copy its host, port, username and password.
2. On [Render](https://render.com): **New > Blueprint**, pick this repo. Render reads [`render.yaml`](render.yaml) and asks for `FALKORDB_HOST`, `FALKORDB_PORT`, `FALKORDB_USERNAME`, `FALKORDB_PASSWORD` (and optionally `ANTHROPIC_API_KEY`). Set `FALKORDB_TLS=true` if your instance requires TLS.
3. Deploy. The app seeds the demo data into FalkorDB on first start.

### Tests and the concurrency check

```bash
python -m pytest -q                  # 9 end-to-end tests against a real FalkorDB
python scripts/concurrency.py        # claim race, 16-thread load, tenant isolation
```

Sample output on a laptop-class container:

```
claim race: 30 agents -> owners seen {'technical'}, ASSIGNED_TO edges 1
load: 16 threads, 3200 ops in 8.19s = 391 ops/s
  write: n=1615 p50=13.2ms p95=41.5ms
  read : n=1585 p50=59.8ms p95=109.3ms
  duplicate memories after concurrent MERGE: 0
isolation: memory written to load-a visible in load-b: 0
```

## LLM providers

The agents use the LLM for two jobs: understanding the message (topic, entities, facts worth
remembering, which tools to call) and wording the reply. Facts always come from the graph.

| `LLM_PROVIDER` | Key | Default model |
|---|---|---|
| `anthropic` (default when a key is set) | `ANTHROPIC_API_KEY` | `claude-opus-5-5` |
| `openai` | `OPENAI_API_KEY` | `gpt-5` |
| `gemini` | `GEMINI_API_KEY` | `gemini-2.5-flash` |
| `offline` (default with no key) | none | rule-based planner and graph-built answers |

Override the model with `LLM_MODEL`. Guardrails:

- **The LLM never writes Cypher.** It picks from a fixed catalogue of parameterised tools
  ([`recalldesk/tools.py`](recalldesk/tools.py)). `customer_id` is always forced to the real customer.
- **Path-checked answers.** Any `INV-`, `INC-`, `KB-` or `TK-` id in the LLM's reply must appear in the
  graph results or recalled memory. If one doesn't, the reply is discarded and an answer built from the
  rows is sent instead. The trace records that this happened.
- **Honest outcomes** on every reply: `answered with graph path`, `answered from memory`, `not in graph`.

## Graph data model

One graph per company. Labels and relationships:

```mermaid
graph LR
  subgraph Company knowledge
    Customer -- SUBSCRIBES_TO --> Plan -- INCLUDES --> Feature -- DEPENDS_ON --> Service
    Service -- DEPENDS_ON --> Service
    Incident -- ON --> Service
    Incident -- IN_REGION --> Region
    Customer -- IN_REGION --> Region
    Customer -- BILLED --> Invoice
    Customer -- PAYS_WITH --> PaymentMethod
    Article -- ABOUT --> Feature
    Refund -- FOR_INVOICE --> Invoice
  end
  subgraph Episodic memory
    Session -- WITH --> Customer
    Session -- HAS_TURN --> Turn -- NEXT --> Turn
    Turn -- MENTIONS --> Feature
    Session -- FOLLOWED_BY --> Session
  end
  subgraph Semantic memory
    Customer -- HAS_MEMORY --> Memory
    Memory -- LEARNED_IN --> Session
    Memory -- WRITTEN_BY --> Agent
    Memory -- ABOUT --> Invoice
  end
  subgraph Procedural memory
    Procedure -- FOR_TOPIC --> Topic
  end
  subgraph Coordination
    Agent -- CAN_HANDLE --> Topic
    Customer -- OPENED --> Ticket
    Ticket -- ASSIGNED_TO --> Agent
    Ticket -- ABOUT --> Feature
    Ticket -- CAUSED_BY --> Incident
    Ticket -- USED_PROCEDURE --> Procedure
    Ticket -- IN_SESSION --> Session
    Handoff -- FOR --> Ticket
    Handoff -- FROM --> Agent
    Handoff -- TO --> Agent
    Handoff -- CARRIES --> Memory
  end
```

| Node | Key properties |
|---|---|
| `Customer` | `id`, `name`, `email` (all synthetic) |
| `Plan` / `Feature` / `Service` / `Region` | `name` (`Plan.price`) |
| `Incident` | `id`, `title`, `status` (`open`/`resolved`), `started` |
| `Invoice` | `id`, `amount`, `status` (`paid`/`failed`/`overdue`/`refund_pending`), `period` |
| `Session` / `Turn` | `id`, `started` / `role`, `agent`, `text`, `ts` |
| `Memory` | `id`, `kind` (`promise`/`fact`/`preference`/`context`), `text`, `hits`, `created` |
| `Ticket` | `id`, `topic`, `status`, `summary`, `owner` |
| `Handoff` | `id`, `reason`, `ts` |
| `Procedure` | `name`, `steps[]`, `successes`, `failures` |
| `Agent` / `Topic` | `name` (`Agent.role`) |

Indexes: `Customer.id`, `Feature.name`, `Service.name`, `Ticket.id`, `Session.id`, `Memory.id`,
`Agent.name`, `Invoice.id`, `Topic.name`.

## Cypher queries

Every query the product depends on is in **[docs/cypher.md](docs/cypher.md)**, generated from the source.
The core one, the multi-hop outage trace:

```cypher
MATCH (c:Customer {id:$customer_id})-[:IN_REGION]->(r:Region)
MATCH p=(c)-[:SUBSCRIBES_TO]->(:Plan)-[:INCLUDES]->(f:Feature)-[:DEPENDS_ON*1..6]->(s:Service)
        <-[:ON]-(i:Incident {status:'open'})-[:IN_REGION]->(r)
WHERE toLower(f.name) = toLower($feature)
RETURN f.name AS feature, s.name AS root_service, i.id AS incident, length(p) AS hops, p
ORDER BY hops LIMIT 3
```

Shared memory handed to the next agent:

```cypher
MATCH (t:Ticket {id:$tid})<-[:OPENED]-(c:Customer), (f:Agent {name:$from}), (to:Agent {name:$to})
CREATE (h:Handoff {id:$hid, reason:$reason, ts:$ts})
CREATE (h)-[:FROM]->(f), (h)-[:TO]->(to), (h)-[:FOR]->(t)
WITH h, c
OPTIONAL MATCH (c)-[:HAS_MEMORY]->(m:Memory)
FOREACH (x IN CASE WHEN m IS NULL THEN [] ELSE [m] END | MERGE (h)-[:CARRIES]->(x))
```

Procedural memory, picking the playbook with the best smoothed success rate:

```cypher
MATCH (p:Procedure)-[:FOR_TOPIC]->(:Topic {name:$topic})
WITH p, (p.successes + 1.0) / (p.successes + p.failures + 2.0) AS rate
RETURN p.name, p.steps, round(rate * 100) AS success_rate ORDER BY rate DESC LIMIT 1
```

## Architecture

```
browser (static/index.html: chat · graph path · agent trace · memory graph)
   │  REST
FastAPI (recalldesk/server.py)
   │
agents.py ── triage ──► understand ─► remember ─► ticket ─► route (CAN_HANDLE) ─► handoff
   │           billing / technical ──► read handoff ─► graph tools ─► playbook ─► act ─► answer ─► path check
   │                                                    │
   ├── tools.py   fixed, parameterised Cypher catalogue │
   ├── memory.py  episodic / semantic / procedural / coordination queries
   ├── llm.py     Claude · GPT · Gemini · offline
   └── db.py      FalkorDB client, one graph per tenant, path → nodes/edges for the UI
                                │
                        FalkorDB (Docker or Cloud)
```

Each reply returns the answer, the outcome, every step with its Cypher, parameters, row count and
latency, and the nodes and edges those queries touched. The UI draws them.

![Agent trace](docs/agent-trace.png)
![Memory graph](docs/memory-graph.png)

## API

| Method | Path | Body | Returns |
|---|---|---|---|
| `GET` | `/api/tenants` | | companies and their customers |
| `POST` | `/api/session` | `{tenant, customer_id}` | `session_id` and the triage greeting (from memory) |
| `POST` | `/api/chat` | `{tenant, customer_id, session_id, message}` | `answer`, `agent`, `outcome`, `ticket`, `trace[]`, `graph{nodes,edges}`, `memory` |
| `POST` | `/api/feedback` | `{tenant, ticket_id, resolved}` | updated ticket and playbook counts |
| `GET` | `/api/memory/{tenant}/{customer_id}` | | the customer's memory subgraph |
| `POST` | `/api/reset` | | re-seeds the demo data |

## Data

All data is synthetic and lives in [`recalldesk/seed.py`](recalldesk/seed.py): two fictional companies
(Acme Cloud Drive and Globex Telecom), made-up customers with `@example.com` addresses, invoices,
services, incidents, help articles and playbooks. No real or personal data is used.

## AI use disclosure

This project was built during the hackathon window with **Claude Code** (Anthropic) as an AI coding
assistant, which helped write the code, tests and docs. The graph model, scenario and design choices
were directed and reviewed by the author. At runtime the agents can call Claude, GPT or Gemini; they
also run fully offline with no model at all.

## Project layout

```
recalldesk/  db.py  seed.py  tools.py  memory.py  agents.py  llm.py  server.py
static/      index.html, vendor/vis-network (MIT / Apache-2.0)
scripts/     concurrency.py, gen_cypher_docs.py
tests/       test_desk.py
docs/        cypher.md, screenshots
```

## License

MIT
