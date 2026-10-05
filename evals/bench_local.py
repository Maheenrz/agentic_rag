import sys, time
from langchain_ollama import ChatOllama

PROMPT = "Explain in 5 sentences what session hijacking is."

for model in sys.argv[1:]:
    llm = ChatOllama(model=model, temperature=0, num_predict=200)
    t0 = time.perf_counter()
    r = llm.invoke(PROMPT)
    wall = time.perf_counter() - t0
    m = r.response_metadata
    toks = m.get("eval_count", 0)
    gen_s = m.get("eval_duration", 0) / 1e9
    print(f"{model}: wall={wall:.1f}s tokens={toks} "
          f"gen_tok/s={toks / gen_s if gen_s else 0:.1f} "
          f"load={m.get('load_duration', 0) / 1e9:.1f}s")