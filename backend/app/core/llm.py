import os
from functools import lru_cache

from langchain_groq import ChatGroq

from app.config import GENERATION_MODEL, SMALL_MODEL


def _names(var: str) -> list[str]:
    return [m.strip() for m in os.getenv(var, "").split(",") if m.strip()]


FALLBACKS = {
    "small": _names("GROQ_SMALL_FALLBACKS"),
    "generation": _names("GROQ_GEN_FALLBACKS"),
}


@lru_cache(maxsize=4)
def get_llm(role: str):
    """role: 'small' (cheap tasks) or 'generation' (the final answer)."""
    primary_name = SMALL_MODEL if role == "small" else GENERATION_MODEL
    primary = ChatGroq(model=primary_name, temperature=0, max_retries=1)
    fallbacks = [ChatGroq(model=m, temperature=0, max_retries=1) for m in FALLBACKS[role]]
    return primary.with_fallbacks(fallbacks) if fallbacks else primary