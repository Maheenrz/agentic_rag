"""
Measures the rule-based guardrail layers against redteam/cases.py. No LLM, no network.

    python redteam/run_redteam.py            # summary tables
    python redteam/run_redteam.py --verbose  # also list every miss / false positive

Reads like an eval report: counts, rates with 95% Wilson confidence intervals,
dev and test reported separately. Quote the TEST numbers.
"""
import argparse, math, os, sys
os.environ.setdefault("GROQ_API_KEY", "x")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.guardrails import security as S
from app.guardrails import pii
from cases import DOC_ATTACKS, DOC_BENIGN, DOC_EDUCATION, QUESTIONS, OUTPUTS, PII


def wilson(k, n, z=1.96):
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def fmt(k, n):
    if n == 0:
        return "   n/a"
    lo, hi = wilson(k, n)
    return f"{k:>2}/{n:<2} = {100 * k / n:5.1f}%  (CI {100 * lo:3.0f}-{100 * hi:3.0f}%)"


def severity_rank(findings):
    return 2 if any(f.severity == S.HIGH for f in findings) else 1 if findings else 0


def doc_outcome(text):
    return severity_rank(S.scan_text(text))              # 2 = block, 1 = flag, 0 = clean


def question_outcome(q):
    allowed, _reason, flags = S.check_user_input(q)
    if not allowed:
        return 2
    return 1 if flags else 0


RANK = {"block": 2, "flag": 1, "pass": 0}


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--verbose", action="store_true"); a = ap.parse_args()
    problems = []
    print("=" * 78); print("RED-TEAM REPORT (rule-based layers)"); print("=" * 78)

    for split in ("dev", "test"):
        print(f"\n--- split: {split.upper()} " + ("(used for tuning)" if split == "dev" else "(NOT used for tuning: quote these)"))
        # ---- document scan
        att = [c for c in DOC_ATTACKS if c[1] == split]
        caught_flag = [c for c in att if doc_outcome(c[3]) >= 1]
        caught_block = [c for c in att if doc_outcome(c[3]) >= 2]
        met = [c for c in att if doc_outcome(c[3]) >= RANK[c[4]]]
        ben = [c for c in DOC_BENIGN if c[1] == split]
        fp_block = [c for c in ben if doc_outcome(c[3]) >= 2]
        fp_flag = [c for c in ben if doc_outcome(c[3]) == 1]
        edu = [c for c in DOC_EDUCATION if c[1] == split]
        print("DOCUMENT SCAN (ingest + query-time)")
        print("  attacks detected at all     :", fmt(len(caught_flag), len(att)))
        print("  attacks BLOCKED (HIGH)      :", fmt(len(caught_block), len(att)))
        print("  attacks at expected level   :", fmt(len(met), len(att)))
        print("  benign wrongly BLOCKED (FP) :", fmt(len(fp_block), len(ben)), "<- this stops real uploads")
        print("  benign wrongly flagged      :", fmt(len(fp_flag), len(ben)), "<- warning only")
        print("  security-education text blocked by design:", f"{sum(doc_outcome(c[3]) >= 2 for c in edu)}/{len(edu)}")
        by_cat = {}
        for c in att:
            by_cat.setdefault(c[2], [0, 0]); by_cat[c[2]][1] += 1; by_cat[c[2]][0] += doc_outcome(c[3]) >= RANK[c[4]]
        print("  by category (expected level):", ", ".join(f"{k} {v[0]}/{v[1]}" for k, v in sorted(by_cat.items())))
        problems += [(split, "doc MISS", c[0], c[2], c[3][:70].replace("\n", " ")) for c in att if c not in met]
        problems += [(split, "doc FALSE-BLOCK", c[0], c[2], c[3][:70]) for c in fp_block]

        # ---- question gate
        qs = [c for c in QUESTIONS if c[1] == split]
        qa = [c for c in qs if c[4] != "pass"]; qb = [c for c in qs if c[4] == "pass"]
        q_met = [c for c in qa if question_outcome(c[3]) >= RANK[c[4]]]
        q_fp_block = [c for c in qb if question_outcome(c[3]) == 2]; q_fp_flag = [c for c in qb if question_outcome(c[3]) == 1]
        print("QUESTION GATE")
        print("  attacks at expected level   :", fmt(len(q_met), len(qa)))
        print("  normal questions BLOCKED    :", fmt(len(q_fp_block), len(qb)))
        print("  normal questions flagged    :", fmt(len(q_fp_flag), len(qb)))
        problems += [(split, "question MISS", c[0], c[2], c[3][:70]) for c in qa if c not in q_met]
        problems += [(split, "question FALSE-BLOCK", c[0], c[2], c[3][:70]) for c in q_fp_block]

        # ---- output guard
        outs = [c for c in OUTPUTS if c[1] == split]; ok = []
        for cid, _s, cat, text, ctx, exp in outs:
            text = text.replace("{CANARY}", S.SYSTEM_CANARY)
            cleaned, _ = S.sanitize_answer(text, ctx)
            good = (cleaned == S.POLICY_BLOCK_MESSAGE) if exp.get("blocked") else (
                all(x not in cleaned for x in exp["absent"]) and all(x in cleaned for x in exp["present"]))
            if good: ok.append(cid)
            else: problems.append((split, "output FAIL", cid, cat, cleaned[:70]))
        print("OUTPUT GUARD")
        print("  outputs sanitised correctly :", fmt(len(ok), len(outs)))

        # ---- PII
        ps = [c for c in PII if c[1] == split]; ok = []
        for cid, _s, cat, text, want in ps:
            got = sorted(p.kind for p in pii.find_pii(text))
            if got == sorted(want): ok.append(cid)
            else: problems.append((split, "pii FAIL", cid, cat, f"want {want} got {got}"))
        pos = [c for c in ps if c[4]]; neg = [c for c in ps if not c[4]]
        print("PII DETECTION")
        print("  exactly right               :", fmt(len(ok), len(ps)), f"(positives {sum(c[0] in ok for c in pos)}/{len(pos)}, negatives {sum(c[0] in ok for c in neg)}/{len(neg)})")

    if a.verbose or problems:
        print("\n--- problems (misses / false positives) ---")
        for p in problems:
            print("  [%s] %-20s %-5s %-14s %s" % p)
    return 0


if __name__ == "__main__":
    sys.exit(main())