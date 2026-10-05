"""
Rewrites flat imports to the new package layout. Run from backend/ (restructure.sh does it for you).

    import org_store                -> from app.stores import org_store
    from config import X            -> from app.config import X
    import graph as G               -> from app.core import graph as G
    import main, re, org_store      -> from app import main  /  import re  /  from app.stores import org_store
Also fixes the module names the tests use to stub llm/reranker, and "import config" strings.
Only touches app/, tests/, redteam/, evaluation/ -- not legacy/.
"""
import pathlib
import re
import sys

PKG = {
    "main": "app", "config": "app", "auth": "app",
    "graph": "app.core", "llm": "app.core", "reranker": "app.core",
    "document_processor": "app.rag", "parsers": "app.rag", "ingest": "app.rag", "quotes": "app.rag",
    "users_store": "app.stores", "org_store": "app.stores", "session_store": "app.stores",
    "feedback_store": "app.stores", "audit_store": "app.stores",
    "security": "app.guardrails", "pii": "app.guardrails", "guard_llm": "app.guardrails",
    "guardrail_policy": "app.guardrails",
}
FROM_RE = re.compile(r"^(\s*)from (" + "|".join(PKG) + r") import ", re.M)
IMPORT_RE = re.compile(r"^(\s*)import ([A-Za-z_][\w]*(?: as \w+)?(?:\s*,\s*[A-Za-z_][\w]*(?: as \w+)?)*)\s*$", re.M)


def rewrite_import_line(m: re.Match) -> str:
    indent, names = m.group(1), [n.strip() for n in m.group(2).split(",")]
    ours, plain = [], []
    for n in names:
        base = n.split(" as ")[0].strip()
        (ours if base in PKG else plain).append(n)
    if not ours:
        return m.group(0)
    lines = [f"{indent}import {', '.join(plain)}"] if plain else []
    lines += [f"{indent}from {PKG[n.split(' as ')[0].strip()]} import {n}" for n in ours]
    return "\n".join(lines)


def fix(path: pathlib.Path) -> bool:
    old = path.read_text(encoding="utf-8")
    new = FROM_RE.sub(lambda m: f"{m.group(1)}from {PKG[m.group(2)]}.{m.group(2)} import ", old)
    new = IMPORT_RE.sub(rewrite_import_line, new)
    if "tests" in path.parts:   # test stubs register fake modules under the real module names
        new = new.replace('sys.modules["llm"]', 'sys.modules["app.core.llm"]').replace('sys.modules["reranker"]', 'sys.modules["app.core.reranker"]')
        new = new.replace('"import config"', '"import app.config"')
    if new != old:
        path.write_text(new, encoding="utf-8")
        print(f"fixed imports: {path}")
        return True
    return False


if __name__ == "__main__":
    root = pathlib.Path(".")
    changed = 0
    for folder in ("app", "tests", "redteam", "evaluation"):
        for p in sorted((root / folder).rglob("*.py")):
            changed += fix(p)
    print(f"{changed} file(s) updated")
    leftover = [str(p) for f in ("app", "tests", "redteam", "evaluation") for p in (root / f).rglob("*.py")
                if re.search(r"^\s*(?:from|import) (?:%s)\b" % "|".join(PKG), p.read_text(encoding="utf-8"), re.M)
                and not re.search(r"from app", p.read_text(encoding="utf-8"))]
    sys.exit(0)