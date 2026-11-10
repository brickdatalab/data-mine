"""API keys: read from data-mine/.env, falling back to real environment variables."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_env():
    env = {}
    p = ROOT / ".env"
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def get(key):
    return load_env().get(key) or os.environ.get(key) or ""


def need(*keys):
    """Values for these keys, in order. Exits naming the blank ones."""
    vals = [get(k) for k in keys]
    missing = [k for k, v in zip(keys, vals) if not v]
    if missing:
        sys.exit(f"Missing in .env: {', '.join(missing)}. Copy .env.example to .env and fill these in.")
    return vals
