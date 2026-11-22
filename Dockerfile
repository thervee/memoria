# Memoria, ready for Railway (or any Docker host).
#
# The image holds only code. No secrets and no data are copied in:
#   - secrets (Gemini key, MEMORIA_SECRET_KEY) come from environment variables
#   - all data lives in /data, where a Railway volume must be mounted
# .dockerignore keeps .env, keys, memory_store and backups out of the build too.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Packages first, so code changes don't reinstall them on every build.
COPY requirements.txt .
RUN pip install -r requirements.txt

# Only the code the server needs (listed one by one, so nothing else slips in).
COPY paths.py setup_cognee.py server.py memoria.py db.py accounts.py identity.py \
     limits.py signup.py dashboard.py demo.py backup.py restore_backup.py manage_accounts.py ./

# Run online: listen on 0.0.0.0 and Railway's PORT, dashboard off, trust Railway's
# proxy for visitors' addresses, and keep every piece of data in /data.
ENV MEMORIA_ONLINE=1 \
    MEMORIA_DATA_DIR=/data

# Keep memory use down on a many-CPU host (Railway shows 32 CPUs): without this, the
# C memory allocator keeps separate memory pools for many threads.
ENV MALLOC_ARENA_MAX=2

# Settings that aren't secret (the same as .env.example). Secrets are NOT set here:
# add LLM_API_KEY and MEMORIA_SECRET_KEY as variables in Railway.
ENV LLM_PROVIDER=gemini \
    LLM_MODEL=gemini/gemini-3.5-flash-lite \
    LLM_RATE_LIMIT_ENABLED=true \
    LLM_RATE_LIMIT_REQUESTS=8 \
    LLM_RATE_LIMIT_INTERVAL=60 \
    EMBEDDING_PROVIDER=fastembed \
    EMBEDDING_MODEL=BAAI/bge-small-en-v1.5 \
    EMBEDDING_DIMENSIONS=384 \
    TELEMETRY_DISABLED=true \
    COGNEE_SKIP_CONNECTION_TEST=true

# Runs as root: Railway mounts volumes owned by root, so another user couldn't write /data.
CMD ["python", "server.py"]
