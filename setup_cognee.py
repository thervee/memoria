"""Loads Memoria's settings and points Cognee at local storage.

Import this before using Cognee anywhere in Memoria. Memoria only imports it the first
time Cognee is actually needed (memoria._engine()), not at start-up, because Cognee and
its search model take a lot of memory.

Memory limits (each can be changed with the environment variable of the same name):
  KUZU_BUFFER_POOL_SIZE   Cognee's graph database cache. Cognee's default is 32 GB,
                          which lets it grow far past a 1 GB server; Memoria uses 128 MB.
  EMBEDDING_BATCH_SIZE    texts the search model turns into vectors at once (default 8)
  EMBEDDING_MAX_CONCURRENT_DATA_POINTS   texts waiting for the model at once (default 16)
  MEMORIA_EMBEDDING_THREADS   CPU threads the search model uses (default 2)
  KUZU_NUM_THREADS        threads the graph database uses (default 2; Cognee's default is
                          one per CPU, and Railway shows 32)
  GRAPH_DATABASE_SUBPROCESS_ENABLED, VECTOR_DB_SUBPROCESS_ENABLED
                          false: Cognee's graph and vector databases run inside Memoria's
                          process. Cognee's default starts a helper process for them (per
                          dataset), each with its own copy of Python and libraries (60-190
                          MB each). Memoria is one process with one background worker, so
                          it doesn't need them.
The search model (fastembed) is also told to give its working memory back after every
batch instead of keeping it for next time.
"""
from pathlib import Path

from dotenv import load_dotenv

import paths

HERE = Path(__file__).resolve().parent
STORE = paths.STORE  # all memories live in this folder (see paths.py)

# Read the .env file (API key and model choices) before Cognee starts.
load_dotenv(HERE / ".env", override=True)

import os  # noqa: E402

# Keep the free local search model inside the Memoria folder too.
os.environ.setdefault("FASTEMBED_CACHE_PATH", str(STORE / "models"))
# Keep Cognee's memory use small (see above). Settings already given win.
os.environ.setdefault("KUZU_BUFFER_POOL_SIZE", str(128 * 1024 * 1024))  # a power of 2
os.environ.setdefault("EMBEDDING_BATCH_SIZE", "8")
os.environ.setdefault("EMBEDDING_MAX_CONCURRENT_DATA_POINTS", "16")
os.environ.setdefault("KUZU_NUM_THREADS", "2")
os.environ.setdefault("GRAPH_DATABASE_SUBPROCESS_ENABLED", "false")
os.environ.setdefault("VECTOR_DB_SUBPROCESS_ENABLED", "false")

import importlib  # noqa: E402

import cognee  # noqa: E402
import fastembed  # noqa: E402

# The module (not the class of the same name) whose TextEmbedding Cognee calls.
_fastembed_engine = importlib.import_module(
    "cognee.infrastructure.databases.vector.embeddings.FastembedEmbeddingEngine")

cognee.config.data_root_directory(str(STORE / "data"))
cognee.config.system_root_directory(str(STORE / "system"))


class _LeanTextEmbedding(fastembed.TextEmbedding):
    """fastembed's search model, set to use few threads and to free its working memory
    after each batch. Cognee creates it once and reuses it (it caches the engine)."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("threads", int(os.environ.get("MEMORIA_EMBEDDING_THREADS", "2")))
        kwargs.setdefault("enable_cpu_mem_arena", False)
        super().__init__(*args, **kwargs)


_fastembed_engine.TextEmbedding = _LeanTextEmbedding
