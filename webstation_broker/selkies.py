"""Token pushes to the selkies control plane.

Selkies enforces input routing itself: it receives the full token map
`{token: {role, slot, mk_control}}` and decides per websocket connection
whether input goes to mouse/keyboard, a virtual gamepad slot, or nowhere. The
broker just keeps that map in sync with room state.
"""

import logging
from typing import Any

import httpx

from . import settings

log = logging.getLogger(__name__)


def build_token_map(session: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Build the token map selkies routes input by, from the session's room state.

    The controller keeps mouse and keyboard control unless a viewer has been
    handed it; a viewer holds it only while they are the `mk_owner_token`.

    Args:
        session: The active session dict.

    Returns:
        A mapping of streaming token to `{"role", "slot", "mk_control"}`, one
        entry for the controller and one per viewer.
    """
    mk_owner = session.get("mk_owner_token")
    controller_token = session["controller_token"]
    tokens = {
        controller_token: {
            "role": "controller",
            "slot": session.get("controller_slot"),
            "mk_control": (mk_owner == controller_token) if mk_owner else True,
        }
    }
    for v in session.get("viewers", []):
        tokens[v["token"]] = {
            "role": "viewer",
            "slot": v.get("slot"),
            "mk_control": v["token"] == mk_owner,
        }
    return tokens


_active_url: str | None = None
"""Endpoint that last accepted a push.

Tried first so steady-state pushes don't re-probe the other image's URL on
every call.
"""


def _token_urls() -> list[str]:
    """Return the candidate token endpoints, last-known-good first.

    Returns:
        The configured endpoints, with `_active_url` moved to the front when it
        is one of them.
    """
    urls = settings.SELKIES_TOKEN_URLS
    if _active_url in urls:
        return [_active_url] + [u for u in urls if u != _active_url]
    return list(urls)


async def push_tokens(session: dict[str, Any]) -> bool:
    """POST the current token set to selkies.

    Replaces the whole active set: removed tokens disconnect their clients,
    changed permissions apply live. Each candidate endpoint is tried in turn and
    the first to accept is remembered for next time.

    Args:
        session: The active session dict the token map is built from.

    Returns:
        True when an endpoint accepted the push, False when every candidate
        failed (the failure is logged, not raised).
    """
    global _active_url
    tokens = build_token_map(session)
    last_exc = None
    async with httpx.AsyncClient(timeout=2.0) as client:
        for url in _token_urls():
            try:
                resp = await client.post(
                    url,
                    json=tokens,
                    headers={"Authorization": f"Bearer {settings.SELKIES_MASTER_TOKEN}"},
                )
                resp.raise_for_status()
                _active_url = url
                return True
            except Exception as exc:
                last_exc = exc
    log.warning("selkies token push failed: %s", last_exc)
    return False


async def clear_tokens() -> bool:
    """Push an empty token set so every streaming client is disconnected.

    Returns:
        True when an endpoint accepted the empty set, False when every candidate
        failed (the failure is logged, not raised).
    """
    global _active_url
    last_exc = None
    async with httpx.AsyncClient(timeout=2.0) as client:
        for url in _token_urls():
            try:
                resp = await client.post(
                    url,
                    json={},
                    headers={"Authorization": f"Bearer {settings.SELKIES_MASTER_TOKEN}"},
                )
                resp.raise_for_status()
                _active_url = url
                return True
            except Exception as exc:
                last_exc = exc
    log.warning("selkies token clear failed: %s", last_exc)
    return False
