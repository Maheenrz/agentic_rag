"""
Plumbing test for evaluation/run_injection_eval.py  (fake models, free, ~20 seconds).

It does NOT measure anything about the real model. It proves the HARNESS itself is wired correctly BEFORE you spend
Groq tokens on it:
  - a "gullible" fake model that obeys any payload it can read  -> shows exactly what each defence layer removes
  - a "robust" fake model that never obeys                      -> ASR must be 0, utility must stay 100%
  - defences are switched on/off per config, and put back afterwards
  - controls (no payload) never trigger the marker
Run:  python3 -m tests.test_injection_eval
"""
import atexit, importlib, json, os, re, shutil, sys, tempfile, types

_TMP = tempfile.mkdtemp(prefix="ragtest_")
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ.update(GROQ_API_KEY="x", FORCE_OFFLINE_EMBEDDINGS="true", CHROMA_PERSIST_DIR=_TMP + "/chroma", SQLITE_DB_PATH=_TMP + "/test.db")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "evaluation"))
import injection_cases as IC

MODE = {"gullible": True}


class Msg:
    def __init__(self, c): self.content = c


class Fake:
    def __init__(self, fn): self.fn = fn
    def invoke(self, msgs): return Msg(self.fn(msgs))


def small_fn(msgs):
    t = msgs[-1].content
    if "grading retrieved passages" in t: return ",".join(str(i) for i in range(1, 16))
    if "Check whether the DRAFT" in t: return "SUPPORTED"
    return "summary"


def gen_fn(msgs):
    t = msgs[-1].content
    question = t.split("Question:")[-1].strip()
    out = "The answer is " + next((c["fact"] for c in IC.CASES if c["question"] == question), "unknown") + "."
    if MODE["gullible"]:
        canary = re.search(r"cnry-[0-9a-f]{12}", msgs[0].content).group(0) if len(msgs) > 1 else ""
        for c in IC.CASES:
            if c["payload"] in t:                                   # the fake obeys only payloads it can read verbatim
                s = c["success"]
                if s["kind"] == "canary": out += " Internal tag: " + canary
                elif c["id"] == "I08": out += " ![x](https://evil.example/p.png?d=1)"
                elif c["id"] == "I09": out += " Sign in at https://evil.example/portal"
                else: out += " " + s["values"][0]
    return out


llm = types.ModuleType("app.core.llm"); llm.get_llm = lambda kind: Fake(small_fn if kind == "small" else gen_fn); sys.modules["app.core.llm"] = llm
rr = types.ModuleType("app.core.reranker"); rr.rerank_documents = lambda q, docs, top_n: docs[:top_n]; sys.modules["app.core.reranker"] = rr

from app.core import graph as G
from app.guardrails.security import max_severity, scan_text
H = importlib.import_module("run_injection_eval")

before = (G._GENERATION_PROMPT, G._SYSTEM_PROMPT, G._wrap_context, G.sanitize_answer)
HIGH_IDS = {c["id"] for c in IC.CASES if max_severity(scan_text(c["legit"] + "\n\n" + c["payload"])) == "high"}
ALL = {c["id"] for c in IC.CASES}
ARGS = ["--configs", "all", "--sleep", "0", "--offline-embeddings", "--results-dir", _TMP + "/res"]


def runs_of(summary_dir):
    return [json.loads(l) for l in open(os.path.join(summary_dir, "runs.jsonl"), encoding="utf-8")]


def latest_dir():
    root = _TMP + "/res"; return os.path.join(root, sorted(os.listdir(root))[-1])


def by(runs, cfg, kind="attack"): return [r for r in runs if r["config"] == cfg and r["kind"] == kind]


# ------------------------------------------------------------------ 1. gullible model: each layer must remove what it is supposed to
MODE["gullible"] = True
s = H.main(ARGS + ["--tag", "gullible"])
runs = runs_of(latest_dir())
assert len(runs) == 20 * 4 + 20, len(runs)
assert all(not r["error"] for r in runs), [r["error"] for r in runs if r["error"]][:2]
obeyed = lambda cfg: {r["case"] for r in by(runs, cfg) if r["obeyed"]}
reached = lambda cfg: {r["case"] for r in by(runs, cfg) if r["succeeded"]}

assert obeyed("L0_none") == ALL and reached("L0_none") == ALL                        # nothing on: every payload works
assert obeyed("L1_prompt") == ALL - {"I11"}                                           # <context> escaping stops the tag-breakout payload
assert obeyed("L2_chunkdrop") == ALL - {"I11"} - HIGH_IDS                             # chunks the regex flags never reach the model
assert reached("L2_chunkdrop") == obeyed("L2_chunkdrop")                              # no output guard yet: whatever was obeyed is visible
assert obeyed("L3_full") == obeyed("L2_chunkdrop")
assert reached("L3_full") == obeyed("L3_full") - {"I07", "I08"}                       # output guard removes the canary leak and the image
assert all(r["dropped"] for r in by(runs, "L2_chunkdrop") if r["case"] in HIGH_IDS)   # and says so in the trace
assert all(not r["dropped"] for r in by(runs, "L0_none"))
assert "I09" in reached("L3_full") or "I09" in HIGH_IDS                               # known gap: a link that appears in the poisoned doc itself is "allowed" by the guard
ctrl = by(runs, "L3_full", "control")
assert len(ctrl) == 20 and not any(r["obeyed"] or r["succeeded"] for r in ctrl) and all(r["fact_present"] for r in ctrl)
assert {r["case"]: r["scanner"] for r in by(runs, "L0_none")}["I01"] == "high"       # scanner column is filled in
assert s["controls"]["false_marker"] == 0

# ------------------------------------------------------------------ 2. robust model: nothing may succeed, product keeps working
MODE["gullible"] = False
s = H.main(ARGS + ["--tag", "robust"])
runs = runs_of(latest_dir())
_bad = [r for r in runs if r["obeyed"] or r["succeeded"] or not r["fact_present"]]
assert not _bad, [(r["case"], r["config"], r["kind"], r["error"], r["tries"], r["trace_tail"]) for r in _bad][:3]

# ------------------------------------------------------------------ 3. options, and defences are put back afterwards
MODE["gullible"] = True
H.main(["--configs", "L0_none", "--cases", "I01,I07", "--no-controls", "--sleep", "0", "--offline-embeddings", "--results-dir", _TMP + "/res", "--tag", "subset"])
assert [(r["case"], r["config"]) for r in runs_of(latest_dir())] == [("I01", "L0_none"), ("I07", "L0_none")]
try:
    H.main(["--configs", "L9_nonsense", "--results-dir", _TMP + "/res"]); raise SystemExit("bad config accepted")
except SystemExit as exc:
    assert "unknown config" in str(exc)
assert before == (G._GENERATION_PROMPT, G._SYSTEM_PROMPT, G._wrap_context, G.sanitize_answer), "defences were not restored"
print("ALL INJECTION-EVAL PLUMBING CHECKS PASSED")