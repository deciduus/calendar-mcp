"""Shared pytest setup.

Sets dummy Google OAuth env vars before any ``src`` module is imported so that
import-time configuration code does not need real credentials, and makes the
repository root importable so ``import src.*`` works no matter where pytest is
invoked from.
"""
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("GOOGLE_CLIENT_ID", "dummy-client-id.apps.googleusercontent.com")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "dummy-client-secret")
os.environ.setdefault("GOOGLE_REDIRECT_URI", "http://localhost:8000/oauth2callback")
