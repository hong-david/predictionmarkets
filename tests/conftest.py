import os
import sys
from pathlib import Path

# Make the project root importable as `app.*` from the tests directory.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Tests must never depend on a developer's real .env. Set safe in-memory
# defaults BEFORE any `app.*` import happens, so pydantic-settings does not
# pick up real credentials.
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("KALSHI_API_KEY_ID", "")
os.environ.setdefault("KALSHI_PRIVATE_KEY_PATH", "")
os.environ.setdefault("POSTGRES_USER", "postgres")
os.environ.setdefault("POSTGRES_PASSWORD", "postgres")
os.environ.setdefault("POSTGRES_DB", "surveillance_test")
os.environ.setdefault("POSTGRES_HOST", "127.0.0.1")
os.environ.setdefault("POSTGRES_PORT", "5432")
