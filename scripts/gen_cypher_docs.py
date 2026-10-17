"""Regenerate docs/cypher.md from the code, so the documented queries are the ones that run.

    python scripts/gen_cypher_docs.py
"""
import ast
import inspect
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from recalldesk import memory, tools  # noqa: E402

MEMORY_FUNCS = ["recall", "remember", "add_turn", "start_session", "open_or_reuse_ticket", "claim_ticket",
                "route", "handoff", "handoff_context", "best_procedure", "record_outcome"]


def cypher_strings(fn) -> list[str]:
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and any(
                k in node.value for k in ("MATCH", "CREATE", "MERGE")):
            out.append(textwrap.dedent(node.value).strip())
    return out


lines = ["# Cypher queries Recall Desk depends on", "",
         "Generated from the source by `scripts/gen_cypher_docs.py`. Every query is parameterised; "
         "the LLM chooses tools and fills arguments but never writes Cypher.", "",
         "## Agent tools (`recalldesk/tools.py`)", ""]
for t in tools.TOOLS.values():
    lines += [f"### `{t.name}`", "", f"{t.description}  ", f"Used by: {', '.join(t.agents)} · params: "
              f"{', '.join('$' + p for p in t.params) or 'none'}", "", "```cypher", textwrap.dedent(t.cypher).strip(), "```", ""]
lines += ["## Memory and coordination (`recalldesk/memory.py`)", ""]
for name in MEMORY_FUNCS:
    fn = getattr(memory, name)
    doc = (inspect.getdoc(fn) or "").split("\n")[0]
    lines += [f"### `memory.{name}()`", "", doc, ""]
    for q in cypher_strings(fn):
        lines += ["```cypher", q, "```", ""]
(ROOT / "docs" / "cypher.md").write_text("\n".join(lines))
print("wrote docs/cypher.md")
