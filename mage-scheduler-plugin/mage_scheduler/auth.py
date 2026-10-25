"""Authentication for the Mage Scheduler control plane.

Ported from the mAgents pattern (mage_agent/auth.py): an auto-generated key file,
constant-time comparison, fail-closed, with a loud env escape hatch for dev. The
scheduler is single-user and loopback-bound, so the cookie scheme is deliberately
minimal — the cookie value IS the api key (HttpOnly), no server-side session store.

Consumers:
  - the MCP server (same user) auto-reads the key file → Mage Lab integration is unchanged
  - the dashboard: GET /login?key=... sets the cookie once
  - external callers (e.g. the SecurityTeam dispatcher): Authorization: Bearer <key>
"""
from __future__ import annotations

import hmac
import logging
import os
import secrets
from pathlib import Path

logger = logging.getLogger("mage_scheduler.auth")

_ENV_KEY = "SCHEDULER_API_KEY"        # pin a key (takes precedence over the file)
_ENV_ANON = "SCHEDULER_ALLOW_ANON"    # =1 disables auth (dev only; logged loudly)
COOKIE_NAME = "mage_scheduler_key"

# Unauthenticated paths: liveness probes, the login page itself, static assets.
_EXEMPT_EXACT = {"/health", "/health/worker", "/login"}
_EXEMPT_PREFIX = ("/static",)


def _key_path() -> Path:
    base = Path(os.environ.get("SCHEDULER_DATA_DIR", Path.home() / ".mage_scheduler"))
    return base / "api.key"


def get_or_create_key() -> str:
    """Return the control-plane key, generating a 0600 file on first run (logged once)."""
    pinned = os.environ.get(_ENV_KEY)
    if pinned:
        return pinned
    path = _key_path()
    try:
        if path.exists():
            val = path.read_text().strip()
            if val:
                return val
    except OSError as exc:
        logger.warning("could not read scheduler api key (%s); regenerating", exc)
    key = secrets.token_urlsafe(32)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(key)
        path.chmod(0o600)
        logger.warning("Generated a new Mage Scheduler API key at %s (mode 0600). "
                       "Retrieve it with: cat %s", path, path)
    except OSError as exc:
        logger.error("could not persist scheduler api key (%s); using ephemeral key", exc)
    return key


def anon_enabled() -> bool:
    if os.environ.get(_ENV_ANON) == "1":
        logger.warning("SCHEDULER_ALLOW_ANON=1 — control plane auth is DISABLED (dev only)")
        return True
    return False


def _present_token(request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    # query-param key: the ONLY credential channel that works inside Mage Lab's cross-origin
    # sandboxed dashboard iframe (SameSite cookies are dropped in that third-party framing; the
    # base.html shim threads ?key= through every link/form). Equivalent security to the bearer
    # on a loopback single-user tool; uvicorn runs at log-level warning so it never hits logs.
    q = request.query_params.get("key")
    if q:
        return q
    return request.cookies.get(COOKIE_NAME)


def is_exempt(path: str) -> bool:
    return path in _EXEMPT_EXACT or path.startswith(_EXEMPT_PREFIX)


def request_allowed(request) -> bool:
    """True if the request may proceed. Fail-closed: anything unrecognized is denied."""
    if is_exempt(request.url.path):
        return True
    if anon_enabled():
        return True
    token = _present_token(request)
    if not token:
        return False
    return hmac.compare_digest(token, get_or_create_key())
