# Cypher queries Recall Desk depends on

Generated from the source by `scripts/gen_cypher_docs.py`. Every query is parameterised; the LLM chooses tools and fills arguments but never writes Cypher.

## Agent tools (`recalldesk/tools.py`)

### `customer_profile`

Who the customer is: plan, region, features their plan includes.  
Used by: triage, billing, technical · params: $customer_id

```cypher
MATCH p=(c:Customer {id:$customer_id})-[:SUBSCRIBES_TO]->(pl:Plan)-[:INCLUDES]->(f:Feature)
MATCH (c)-[:IN_REGION]->(r:Region)
RETURN c.name AS customer, pl.name AS plan, r.name AS region, collect(f.name) AS features, collect(p) AS paths
```

### `billing_overview`

All invoices and payment methods for the customer.  
Used by: billing · params: $customer_id

```cypher
MATCH p=(c:Customer {id:$customer_id})-[:BILLED]->(i:Invoice)
OPTIONAL MATCH pm=(c)-[:PAYS_WITH]->(m:PaymentMethod)
RETURN i.id AS invoice, i.amount AS amount, i.status AS status, i.period AS period,
       m.last4 AS card_last4, m.expired AS card_expired, p, pm
ORDER BY i.period, i.id
```

### `find_duplicate_charges`

Pairs of paid invoices for the same customer, same period and same amount (a double charge).  
Used by: billing · params: $customer_id

```cypher
MATCH p1=(c:Customer {id:$customer_id})-[:BILLED]->(a:Invoice {status:'paid'}),
      p2=(c)-[:BILLED]->(b:Invoice {status:'paid'})
WHERE a.id < b.id AND a.period = b.period AND a.amount = b.amount AND a.kind = b.kind
RETURN a.id AS first_invoice, b.id AS duplicate_invoice, a.amount AS amount, a.period AS period, p1, p2
```

### `failed_payments`

Failed or overdue invoices and whether the card on file has expired.  
Used by: billing · params: $customer_id

```cypher
MATCH p=(c:Customer {id:$customer_id})-[:BILLED]->(i:Invoice)
WHERE i.status IN ['failed', 'overdue']
OPTIONAL MATCH pm=(c)-[:PAYS_WITH]->(m:PaymentMethod)
RETURN i.id AS invoice, i.status AS status, i.amount AS amount, m.last4 AS card_last4, m.expired AS card_expired, p, pm
```

### `trace_feature_outage`

Multi-hop: follow the customer's plan to a feature, through every service it transitively depends on, to any OPEN incident in the customer's region. Returns the full dependency path.  
Used by: technical · params: $customer_id, $feature

```cypher
MATCH (c:Customer {id:$customer_id})-[:IN_REGION]->(r:Region)
MATCH p=(c)-[:SUBSCRIBES_TO]->(:Plan)-[:INCLUDES]->(f:Feature)-[:DEPENDS_ON*1..6]->(s:Service)<-[:ON]-(i:Incident {status:'open'})-[:IN_REGION]->(r)
WHERE toLower(f.name) = toLower($feature)
RETURN f.name AS feature, s.name AS root_service, i.id AS incident, i.title AS incident_title,
       i.started AS started, length(p) AS hops, p
ORDER BY hops
LIMIT 3
```

### `feature_dependencies`

Every service a feature depends on, directly or transitively, with hop depth.  
Used by: technical · params: $feature

```cypher
MATCH p=(f:Feature)-[:DEPENDS_ON*1..6]->(s:Service)
WHERE toLower(f.name) = toLower($feature)
RETURN s.name AS service, length(p) AS depth, p
ORDER BY depth
```

### `incident_blast_radius`

Reverse traversal: which customers in the incident's region are affected through any feature that depends on the broken service.  
Used by: technical · params: $incident_id

```cypher
MATCH (i:Incident {id:$incident_id})-[:ON]->(s:Service), (i)-[:IN_REGION]->(r:Region)
MATCH p=(c:Customer)-[:SUBSCRIBES_TO]->(:Plan)-[:INCLUDES]->(f:Feature)-[:DEPENDS_ON*1..6]->(s)
WHERE (c)-[:IN_REGION]->(r)
RETURN c.name AS customer, collect(DISTINCT f.name) AS via_features, count(DISTINCT p) AS paths
ORDER BY customer
```

### `critical_services`

PageRank over the service dependency graph: the services most other things rely on.  
Used by: technical · params: none

```cypher
CALL algo.pageRank('Service', 'DEPENDS_ON') YIELD node, score
RETURN node.name AS service, round(score * 1000) / 1000 AS score
ORDER BY score DESC LIMIT 5
```

### `related_tickets`

Other customers' tickets whose feature shares a service dependency with this feature (the same root cause, seen by others).  
Used by: technical, triage · params: $customer_id, $feature

```cypher
MATCH (f:Feature)-[:DEPENDS_ON*1..6]->(s:Service)
WHERE toLower(f.name) = toLower($feature)
MATCH p=(t:Ticket)-[:ABOUT]->(of:Feature)-[:DEPENDS_ON*1..6]->(s)
MATCH (other:Customer)-[:OPENED]->(t)
WHERE other.id <> $customer_id
RETURN t.id AS ticket, other.name AS customer, of.name AS feature, collect(DISTINCT s.name) AS shared_services,
       t.status AS status
ORDER BY ticket LIMIT 10
```

### `kb_articles`

Help-centre articles about a feature or topic.  
Used by: technical, billing · params: $about

```cypher
MATCH p=(a:Article)-[:ABOUT]->(x)
WHERE toLower(x.name) = toLower($about)
RETURN a.id AS article, a.title AS title, a.body AS body, p
```

## Memory and coordination (`recalldesk/memory.py`)

### `memory.recall()`

What the desk remembers about a customer (most recent first), from any past session and any agent.

```cypher
MATCH (c:Customer {id:$cid})
OPTIONAL MATCH mp=(c)-[:HAS_MEMORY]->(m:Memory)-[:WRITTEN_BY]->(a:Agent)
WITH c, m, a, mp ORDER BY coalesce(m.last_seen, m.created) DESC LIMIT $limit
WITH c, collect(DISTINCT {kind:m.kind, text:m.text, by:a.name, at:m.created, hits:m.hits}) AS memories,
     collect(mp) AS mpaths
OPTIONAL MATCH tp=(c)-[:OPENED]->(t:Ticket)-[:ASSIGNED_TO]->(owner:Agent)
WITH c, memories, mpaths, collect(DISTINCT {id:t.id, topic:t.topic, status:t.status, summary:t.summary,
     owner:owner.name, created:t.created}) AS tickets, collect(tp) AS tpaths
OPTIONAL MATCH (c)<-[:WITH]-(s:Session)
RETURN [x IN memories WHERE x.text IS NOT NULL] AS memories,
       [x IN tickets WHERE x.id IS NOT NULL] AS tickets,
       count(DISTINCT s) AS sessions, mpaths, tpaths
```

### `memory.remember()`

Store a fact about the customer. Identical facts are merged, not duplicated.

```cypher
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
```

### `memory.add_turn()`

Append a turn to the session chain and link it to the graph entities it mentioned.

```cypher
MATCH (s:Session {id:$sid})
OPTIONAL MATCH (s)-[:HAS_TURN]->(last:Turn) WHERE NOT (last)-[:NEXT]->(:Turn)
CREATE (s)-[:HAS_TURN]->(t:Turn {id:$tid, role:$role, agent:$agent, text:$text, ts:$ts})
FOREACH (_ IN CASE WHEN last IS NULL THEN [] ELSE [1] END | CREATE (last)-[:NEXT]->(t))
WITH t
UNWIND $mentions AS m
MATCH (x) WHERE (x:Feature AND x.name = m) OR (x:Invoice AND x.id = m) OR (x:Incident AND x.id = m)
             OR (x:Service AND x.name = m) OR (x:Article AND x.id = m)
MERGE (t)-[:MENTIONS]->(x)
```

### `memory.start_session()`



```cypher
MATCH (c:Customer {id:$cid})
CREATE (s:Session {id:$sid, started:$ts})-[:WITH]->(c)
WITH s, c
OPTIONAL MATCH (c)<-[:WITH]-(prev:Session) WHERE prev.id <> $sid
WITH s, prev ORDER BY prev.started DESC LIMIT 1
FOREACH (_ IN CASE WHEN prev IS NULL THEN [] ELSE [1] END | CREATE (prev)-[:FOLLOWED_BY]->(s))
```

### `memory.open_or_reuse_ticket()`

Reuse the customer's open ticket on this topic (continuity across sessions) or open a new one.

```cypher
MATCH (c:Customer {id:$cid})-[:OPENED]->(t:Ticket {topic:$topic})
WHERE t.status IN ['open', 'reopened']
RETURN t.id AS id, t.summary AS summary ORDER BY t.created DESC LIMIT 1
```

```cypher
MATCH (t:Ticket {id:$tid}) UNWIND $about AS a
MATCH (x) WHERE (x:Feature AND x.name = a) OR (x:Invoice AND x.id = a)
MERGE (t)-[:ABOUT]->(x)
```

```cypher
MATCH (t:Ticket {id:$tid}), (s:Session {id:$sid}) MERGE (t)-[:IN_SESSION]->(s)
```

```cypher
MATCH (c:Customer {id:$cid}), (s:Session {id:$sid})
CREATE (c)-[:OPENED]->(t:Ticket {id:$tid, topic:$topic, status:'open', summary:$summary, created:$ts})-[:IN_SESSION]->(s)
```

### `memory.claim_ticket()`

Atomic claim. FalkorDB runs each write query atomically per graph, so if many agents

```cypher
MATCH (t:Ticket {id:$tid})
SET t.owner = coalesce(t.owner, $agent)
WITH t
MATCH (a:Agent {name:t.owner})
MERGE (t)-[:ASSIGNED_TO]->(a)
RETURN t.owner AS owner
```

### `memory.route()`



```cypher
MATCH (a:Agent)-[:CAN_HANDLE]->(:Topic {name:$t}) RETURN a.name AS agent LIMIT 1
```

### `memory.handoff()`

Record a handoff and attach the customer's memories to it, so the receiving agent

```cypher
MATCH (t:Ticket {id:$tid})<-[:OPENED]-(c:Customer), (f:Agent {name:$from}), (to:Agent {name:$to})
CREATE (h:Handoff {id:$hid, reason:$reason, ts:$ts})
CREATE (h)-[:FROM]->(f), (h)-[:TO]->(to), (h)-[:FOR]->(t)
WITH h, c
OPTIONAL MATCH (c)-[:HAS_MEMORY]->(m:Memory)
FOREACH (x IN CASE WHEN m IS NULL THEN [] ELSE [m] END | MERGE (h)-[:CARRIES]->(x))
RETURN h.id AS id, count(m) AS carried
```

### `memory.handoff_context()`

What the receiving agent reads: the memories carried by every handoff on this ticket.

```cypher
MATCH p=(f:Agent)<-[:FROM]-(h:Handoff)-[:FOR]->(t:Ticket {id:$tid})
OPTIONAL MATCH (h)-[:CARRIES]->(m:Memory)
RETURN h.id AS handoff, h.ts AS at, f.name AS from_agent, h.reason AS reason, collect(m.text) AS carried, p
ORDER BY at DESC LIMIT 3
```

### `memory.best_procedure()`

Pick the procedure for a topic with the best outcome record (Laplace-smoothed success rate).

```cypher
MATCH (p:Procedure)-[:FOR_TOPIC]->(:Topic {name:$topic})
WITH p, (p.successes + 1.0) / (p.successes + p.failures + 2.0) AS rate
RETURN p.name AS name, p.steps AS steps, p.successes AS successes, p.failures AS failures,
       round(rate * 100) AS success_rate
ORDER BY rate DESC LIMIT 1
```

### `memory.record_outcome()`

Close the loop: the outcome of a ticket updates the procedure that was used.

```cypher
MATCH (t:Ticket {id:$tid})
SET t.status = CASE WHEN $ok THEN 'resolved' ELSE 'reopened' END, t.closed = $ts
WITH t
OPTIONAL MATCH (t)-[:USED_PROCEDURE]->(p:Procedure)
FOREACH (_ IN CASE WHEN p IS NULL OR NOT $ok THEN [] ELSE [1] END | SET p.successes = p.successes + 1)
FOREACH (_ IN CASE WHEN p IS NULL OR $ok THEN [] ELSE [1] END | SET p.failures = p.failures + 1)
RETURN t.id AS ticket, t.status AS status, p.name AS procedure, p.successes AS successes, p.failures AS failures
```
