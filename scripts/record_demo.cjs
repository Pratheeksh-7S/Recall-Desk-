// Records the captioned demo walkthrough. Needs the app on :8000 and Playwright:
//   npm i playwright && npx playwright install chromium && node scripts/record_demo.cjs
//   ffmpeg -i demo-video/*.webm -c:v libx264 -pix_fmt yuv420p demo.mp4
const { chromium } = require('playwright');
const W = 1600, H = 900;
const sleep = ms => new Promise(r => setTimeout(r, ms));
(async () => {
  const b = await chromium.launch();
  const ctx = await b.newContext({ viewport: { width: W, height: H }, recordVideo: { dir: process.env.OUT_DIR || 'demo-video', size: { width: W, height: H } } });
  const p = await ctx.newPage();
  await p.goto('http://localhost:8000/');
  await p.waitForTimeout(2000);
  await p.addStyleTag({ content: `
    #cap { position: fixed; left: 50%; bottom: 110px; transform: translateX(-50%); max-width: 1100px; z-index: 9999;
      background: rgba(10,12,16,.92); color: #fff; font: 600 22px/1.35 system-ui, sans-serif; padding: 14px 22px;
      border-radius: 12px; border: 1px solid #ff6b4a; text-align: center; pointer-events: none; box-shadow: 0 8px 30px rgba(0,0,0,.5); transition: opacity .3s; }
    #cap small { display: block; font-weight: 400; font-size: 16px; color: #c9d1e3; margin-top: 4px; }
    #card { position: fixed; inset: 0; z-index: 10000; background: #0f1115; display: flex; flex-direction: column;
      align-items: center; justify-content: center; color: #e6e8ee; font-family: system-ui, sans-serif; transition: opacity .6s; }
    #card h1 { font-size: 64px; margin: 0; } #card h1 span { color: #ff6b4a; }
    #card p { font-size: 26px; color: #8b93a4; margin: 14px 0 0; text-align: center; max-width: 1100px; }
    .ring { outline: 3px solid #ff6b4a !important; outline-offset: 3px; border-radius: 8px; }` });
  const card = async (title, sub, ms) => {
    await p.evaluate(([t, s]) => { const d = document.createElement('div'); d.id = 'card'; d.innerHTML = `<h1>${t}</h1><p>${s}</p>`; document.body.appendChild(d); }, [title, sub]);
    await sleep(ms);
    await p.evaluate(() => { const d = document.getElementById('card'); d.style.opacity = 0; setTimeout(() => d.remove(), 600); });
    await sleep(700);
  };
  const cap = async (text, sub = '', ms = 4000) => {
    await p.evaluate(([t, s]) => { let d = document.getElementById('cap'); if (!d) { d = document.createElement('div'); d.id = 'cap'; document.body.appendChild(d); }
      d.innerHTML = t + (s ? `<small>${s}</small>` : ''); d.style.opacity = 1; }, [text, sub]);
    await sleep(ms);
  };
  const hideCap = () => p.evaluate(() => { const d = document.getElementById('cap'); if (d) d.style.opacity = 0; });
  const ring = (sel, on = true) => p.evaluate(([s, o]) => document.querySelectorAll(s).forEach(e => e.classList.toggle('ring', o)), [sel, on]);
  const say = async (text) => {
    await p.click('#input');
    await p.type('#input', text, { delay: 35 });
    await sleep(400);
    await p.click('#form button');
    await p.waitForFunction(() => !document.querySelector('#form button').disabled, null, { timeout: 30000 });
    await sleep(900);
  };
  const tab = async name => { await p.click(`.tabs button[data-pane=${name}]`); await sleep(1500); };

  await card('Recall<span>Desk</span>', 'Three AI support agents that share one memory, and that memory is a FalkorDB graph.<br><br>Graph Hacks · Track 02 Agent Memory &amp; Coordination', 5000);

  await cap('Session 1. Priya is on the Team plan in eu-west.', 'Triage reads her memory from the graph before replying. Nothing yet: first visit.', 4500);
  await say('Hi, I think I was charged twice this month');
  await cap('Triage opens a ticket and asks the graph who handles billing.', '(:Agent)-[:CAN_HANDLE]->(:Topic {name:"billing"}) → handoff to the billing agent, carrying her memories', 5000);
  await cap('Billing finds two paid $30 invoices for October and refunds the duplicate.', 'The refund is a graph write, and the promise is stored as a Memory node for every agent to see', 5500);
  await ring('#memories .mem'); await cap('Shared memory, written by the agents themselves', '', 3500); await ring('#memories .mem', false);

  await say('Our Bulk Export has been stuck in queued for hours, urgent!');
  await cap('A different problem goes to the technical agent.', 'It walks Customer → Plan → Feature → export-worker → job-queue → object-store ← Incident → Region: 7 hops', 6000);
  await cap('The open incident INC-301 is in her region, so it is not her fault.', 'Reverse traversal: 4 customers affected, and 2 other tickets share the same root service', 5500);

  await tab('trace');
  await cap('Every answer shows how it was reached.', 'Each agent step, the exact Cypher it ran, its parameters, rows and latency', 3500);
  await p.evaluate(() => { const st = [...document.querySelectorAll('#trace .step')].find(e => e.textContent.includes('trace_feature_outage'));
    if (st) { st.querySelectorAll('details').forEach(d => d.open = true); st.scrollIntoView({ block: 'start', behavior: 'smooth' }); st.classList.add('ring'); } });
  await cap('The 7-hop query, its parameters and the row it returned', '', 6500);
  await p.evaluate(() => document.querySelectorAll('.ring').forEach(e => e.classList.remove('ring')));
  await p.evaluate(() => document.querySelector('#pane-trace').scrollTo({ top: 99999, behavior: 'smooth' }));
  await cap('The LLM picks tools from a fixed catalogue. It never writes Cypher.', 'Any ID in a reply must exist in the graph results, or the reply is replaced with one built from the rows', 5500);
  await tab('path');

  await hideCap();
  await p.click('#newSession'); await sleep(2000);
  await cap('Session 2, later. A brand new conversation.', 'Triage greets Priya with what the desk promised last time. It all comes from the graph, not from app state', 6500);
  await say('Any update on my refund?');
  await cap('Same ticket, carried across sessions, answered from memory.', '', 4500);

  await tab('mem');
  await cap('Her memory graph: sessions, memories, tickets, handoffs and the playbooks used.', 'Episodic, semantic and procedural memory, all in one FalkorDB graph per company', 7000);
  await tab('path');

  await say('Bulk Export is still stuck');
  await cap('Outcomes teach the desk which playbook works.', 'Marking this "Not resolved" updates the Procedure node\'s win/loss record', 3000);
  const nr = await p.$$('#log .meta button');
  if (nr.length) { await nr[nr.length - 1].click(); }
  await sleep(1500);
  await cap('Procedural memory updated.', 'After enough failures the technical agent switches playbooks on its own', 4500);

  await hideCap();
  await p.selectOption('#tenant', 'globex'); await sleep(2500);
  await cap('A second company is a second, isolated graph.', 'Priya at Globex Telecom has the same email, but none of the Acme memories', 5500);

  await hideCap();
  await card('Recall<span>Desk</span>', 'FalkorDB is the memory, the router and the shared state between agents.<br>Multi-hop Cypher · PageRank · per-tenant graphs · atomic claims tested with 30 racing agents<br><br>github.com/Pratheeksh-7S/Recall-Desk-', 6000);
  await ctx.close(); await b.close();
})();
