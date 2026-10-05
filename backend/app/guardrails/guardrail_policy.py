"""
guardrail_policy.py
--------------------
Per-organisation guardrail settings, editable by that org's admins.
Industry guardrail products (Bedrock Guardrails, Azure AI Content Safety,
NeMo Guardrails) work the same way: the rules are CONFIGURATION, not hard-coded,
because different companies need different rules.

Stored as one JSON value in the settings table (key "guardrails:<org_id>").
Anything the admin doesn't set falls back to DEFAULT_POLICY.
"""

import copy
import hashlib
import json

from app.stores import org_store
from app.config import INJECTION_QUERY_ACTION
from app.guardrails.pii import DEFAULT_ACTIONS, KINDS

DEFAULT_POLICY = {
    "pii": dict(DEFAULT_ACTIONS),                    # kind -> allow | mask | block   (question AND answer)
    "blocked_topics": [],                            # phrases the assistant must not discuss in this org
    "injection_query_action": INJECTION_QUERY_ACTION if INJECTION_QUERY_ACTION in ("drop", "flag", "off") else "drop",
    "block_prompt_extraction": True,
    "max_question_chars": 2000,
    "llm_second_opinion": False,                     # ask a small model to double-check SUSPICIOUS questions (costs a call)
}
_ACTIONS = ("allow", "mask", "block")


def _key(org_id: str) -> str:
    return f"guardrails:{org_id}"


def get_policy(org_id: str) -> dict:
    policy = copy.deepcopy(DEFAULT_POLICY)
    raw = org_store.get_setting(_key(org_id))
    if raw:
        try:
            stored = json.loads(raw)
        except ValueError:
            stored = {}
        for k, v in stored.items():
            if k == "pii" and isinstance(v, dict):
                policy["pii"].update({kk: vv for kk, vv in v.items() if kk in KINDS and vv in _ACTIONS})
            elif k in policy:
                policy[k] = v
    return policy


def validate_update(update: dict) -> dict:
    """Checks an admin's partial update; returns the cleaned partial. Raises ValueError with a readable message."""
    clean: dict = {}
    for k, v in update.items():
        if k == "pii":
            if not isinstance(v, dict):
                raise ValueError("pii must be an object like {\"card\": \"mask\"}")
            for kind, action in v.items():
                if kind not in KINDS:
                    raise ValueError(f"unknown pii kind '{kind}' (allowed: {', '.join(KINDS)})")
                if action not in _ACTIONS:
                    raise ValueError(f"pii action for '{kind}' must be one of {', '.join(_ACTIONS)}")
            clean["pii"] = dict(v)
        elif k == "blocked_topics":
            if not isinstance(v, list) or len(v) > 50 or not all(isinstance(t, str) and 2 <= len(t.strip()) <= 80 for t in v):
                raise ValueError("blocked_topics must be a list of up to 50 phrases, 2-80 characters each")
            clean[k] = [t.strip().lower() for t in v]
        elif k == "injection_query_action":
            if v not in ("drop", "flag", "off"):
                raise ValueError("injection_query_action must be drop, flag or off")
            clean[k] = v
        elif k in ("block_prompt_extraction", "llm_second_opinion"):
            if not isinstance(v, bool):
                raise ValueError(f"{k} must be true or false")
            clean[k] = v
        elif k == "max_question_chars":
            if not isinstance(v, int) or isinstance(v, bool) or not 200 <= v <= 8000:
                raise ValueError("max_question_chars must be a whole number between 200 and 8000")
            clean[k] = v
        else:
            raise ValueError(f"unknown setting '{k}'")
    return clean


def update_policy(org_id: str, update: dict) -> dict:
    clean = validate_update(update)
    raw = org_store.get_setting(_key(org_id))
    stored = json.loads(raw) if raw else {}
    if "pii" in clean:
        stored["pii"] = {**stored.get("pii", {}), **clean.pop("pii")}
    stored.update(clean)
    org_store.set_setting(_key(org_id), json.dumps(stored))
    return get_policy(org_id)


def fingerprint(policy: dict) -> str:
    """Goes into the cache key: change the policy and old cached answers (made under the old rules) stop matching."""
    return hashlib.sha1(json.dumps(policy, sort_keys=True).encode()).hexdigest()[:8]