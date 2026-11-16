"""Loads Memoria's settings and points Cognee at local storage.

Import this before using Cognee anywhere in Memoria.
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

import cognee  # noqa: E402

cognee.config.data_root_directory(str(STORE / "data"))
cognee.config.system_root_directory(str(STORE / "system"))
