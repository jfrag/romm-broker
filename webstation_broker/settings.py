"""Environment-driven configuration."""

import os
from pathlib import Path


def _prefix() -> str:
    """Normalized URL prefix the broker is served under, e.g. '/streaming'.
    SUBFOLDER also drives the nginx templating and the vite base."""
    raw = os.environ.get("SUBFOLDER", "/streaming/").strip()
    if not raw.startswith("/"):
        raw = "/" + raw
    return raw.rstrip("/")


PREFIX = _prefix()

HOST = os.environ.get("BROKER_HOST", "127.0.0.1")
PORT = int(os.environ.get("BROKER_PORT", "8000"))

# Shared secret for the session lifecycle endpoints; unset disables auth.
BROKER_SECRET = os.environ.get("BROKER_SECRET", "")

# Selkies control plane for token pushes.
SELKIES_CONTROL_URL = os.environ.get("SELKIES_CONTROL_URL", "http://127.0.0.1:8083")
SELKIES_MASTER_TOKEN = os.environ.get("SELKIES_MASTER_TOKEN", "")

# ROM library root; activate rejects paths outside it.
ROM_ROOT = Path(os.environ.get("ROM_ROOT", "/romm"))

# Where exit writes its save archives (always in dev mode, otherwise only
# when the upload to the callback origin fails).
EXPORT_DIR = Path(os.environ.get("BROKER_EXPORT_DIR", "/config/broker-exports"))

# Where the parent uploads save archives it wants restored; activate's
# save.archive path points into here.
IMPORT_DIR = Path(os.environ.get("BROKER_IMPORT_DIR", "/config/broker-imports"))

# Exit upload target: this path is appended to the callback base URL (the
# parent origin derived at activate unless the payload supplies one).
SAVE_UPLOAD_PATH = os.environ.get("BROKER_SAVE_UPLOAD_PATH", "/api/webstation/saves")
SAVE_UPLOAD_TIMEOUT = float(os.environ.get("BROKER_SAVE_UPLOAD_TIMEOUT", "30"))

# Built frontend served in non-dev mode; ignored when vite serves the page.
FRONTEND_DIST = Path(
    os.environ.get("BROKER_FRONTEND_DIST", "/usr/share/webstation-broker/www")
)

DEV_MODE = os.environ.get("BROKER_DEV_MODE", "").lower() == "true"

GAMEPAD_SLOTS = int(os.environ.get("BROKER_GAMEPAD_SLOTS", "4"))
