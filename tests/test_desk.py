"""End-to-end tests against a real FalkorDB (docker run -p 6379:6379 falkordb/falkordb)."""
import pytest

from recalldesk import agents, db, memory, seed, tools

T, G = "test-acme", "test-globex"


@pytest.fixture(autouse=True)
def fresh_graphs():
    seed.seed_tenant(T, seed.TENANTS["acme"])
    seed.seed_tenant(G, seed.TENANTS["globex"])
    yield
    for t in (T, G):
        db.graph_for(t).delete()


def test_multi_hop_trace_reaches_incident_in_customer_region():
    r = tools.call(T, "trace_feature_outage", {"customer_id": "C-1001", "feature": "Bulk Export"})
    assert r.rows[0]["incident"] == "INC-301"
    assert r.rows[0]["hops"] == 7  # customer > plan > feature > export-worker > job-queue > object-store < incident > region
    # same feature, different region: no incident on the path
    assert tools.call(T, "trace_feature_outage", {"customer_id": "C-1002", "feature": "Sharing Links"}).rows == []


def test_blast_radius_and_pagerank():
    affected = {r["customer"] for r in tools.call(T, "incident_blast_radius", {"incident_id": "INC-301"}).rows}
    assert affected == {"Priya Raman", "Lena Fischer", "Omar Haddad", "Grace Okafor"}
    top = tools.call(T, "critical_services", {}).rows[0]["service"]
    assert top == "object-store"


def test_billing_refund_is_remembered_across_sessions():
    s1 = memory.start_session(T, "C-1001")
    r = agents.handle_message(T, s1, "C-1001", "I was charged twice this month")
    assert r["agent"] == "billing" and "INV-5003" in r["answer"]
    status = db.run(T, "MATCH (i:Invoice {id:'INV-5003'}) RETURN i.status AS s").rows[0]["s"]
    assert status == "refund_pending"

    s2 = memory.start_session(T, "C-1001")
    hello = agents.greet(T, "C-1001", s2)
    assert hello["outcome"] == "answered from memory" and "INV-5003" in hello["answer"]
    follow = agents.handle_message(T, s2, "C-1001", "any update on my refund?")
    assert follow["ticket"] == r["ticket"]  # same ticket carried across sessions
    assert "promised" in follow["answer"]


def test_handoff_carries_memory_to_specialist():
    s = memory.start_session(T, "C-1001")
    r = agents.handle_message(T, s, "C-1001", "I prefer email. Bulk Export is stuck, urgent")
    assert r["agent"] == "technical"
    ctx = memory.handoff_context(T, r["ticket"]).rows[0]
    assert ctx["from_agent"] == "triage"
    assert "Prefers contact by email" in ctx["carried"]
    linked = db.run(T, "MATCH (:Ticket {id:$t})-[:CAUSED_BY]->(i:Incident) RETURN i.id AS i", {"t": r["ticket"]}).rows
    assert linked == [{"i": "INC-301"}]


def test_procedure_outcomes_change_which_playbook_wins():
    assert memory.best_procedure(T, "technical")["name"] == "Diagnose feature outage"
    s = memory.start_session(T, "C-1001")
    used = []
    for _ in range(25):  # the usual playbook keeps failing for this customer
        r = agents.handle_message(T, s, "C-1001", "Bulk Export is broken")
        used.append(r["procedure"]["name"])
        memory.record_outcome(T, r["ticket"], resolved=False)
    assert used[0] == "Diagnose feature outage"
    assert "Restart client" in used  # procedural memory switched playbooks on its own


def test_existing_open_ticket_is_reused_without_new_handoff():
    s = memory.start_session(T, "C-1004")  # Lena already has an open Bulk Export ticket owned by technical
    r = agents.handle_message(T, s, "C-1004", "Bulk Export still stuck")
    assert r["ticket"] == "TK-0001"
    assert not any(st["action"] == "handoff" for st in r["trace"])


def test_tenants_are_isolated():
    s = memory.start_session(T, "C-1001")
    memory.remember(T, "C-1001", s, "triage", "preference", "Acme-only fact")
    assert db.run(G, "MATCH (m:Memory) RETURN count(m) AS n").rows[0]["n"] == 0
    with pytest.raises(ValueError):
        db.graph_for("bad tenant; DROP")


def test_path_check_rejects_invented_ids():
    trace = agents.Trace()
    trace.add("billing", "graph tool", result=tools.call(T, "billing_overview", {"customer_id": "C-1001"}))
    assert agents.path_check("Your invoice INV-5002 is paid", trace, "")
    assert not agents.path_check("Your invoice INV-9999 is paid", trace, "")


def test_unknown_customer_message_without_graph_match():
    s = memory.start_session(T, "C-1003")
    r = agents.handle_message(T, s, "C-1003", "SSO login is not working")
    assert r["agent"] == "technical"
    assert "none has an open incident" in r["answer"]  # no incident in ap-south on SSO's path
