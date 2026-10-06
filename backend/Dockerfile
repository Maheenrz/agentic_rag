# NOT BUILT OR RUN YET: build it locally first (docker build -t docs-api .) before trusting it.
# Python 3.9 reached end of life in Oct 2025 and new langgraph versions need 3.10+.
# If you change the Python version, re-run tests/test_app.py on that version.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/app/models/hf \
    FLASHRANK_CACHE=/app/models/flashrank \
    EMBEDDING_MODEL=/app/models/all-MiniLM-L6-v2

WORKDIR /app

# CPU-only torch: the default wheel pulls ~2 GB of CUDA libraries you cannot use on a small server.
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Download both models at BUILD time, so the server never downloads anything at start-up.
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2').save('/app/models/all-MiniLM-L6-v2')" \
 && python -c "from flashrank import Ranker; Ranker(model_name='ms-marco-MiniLM-L-12-v2', cache_dir='/app/models/flashrank')"

COPY app ./app

# init_db is safe to run on every start (IF NOT EXISTS everywhere).
CMD ["sh", "-c", "python -m app.db && uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]