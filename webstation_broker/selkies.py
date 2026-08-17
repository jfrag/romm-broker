"""Token pushes to the selkies control plane.

Selkies enforces input routing itself: it receives the full token map
{token: {role, slot, mk_control}} and decides per websocket connection whether
input goes to mouse/keyboard, a virtual gamepad slot, or nowhere. The broker
just keeps that map in sync with room state.
"""

import logging

import httpx

from . import settings

log = logging.getLogger(__name__)


def build_token_map(session: dict) -> dict:
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


# Endpoint that last accepted a push; tried first so steady-state pushes don't
# re-probe the other image's URL on every call.
_active_url: str | None = None


def _token_urls() -> list[str]:
    """Candidate token endpoints, last-known-good first."""
    urls = settings.SELKIES_TOKEN_URLS
    if _active_url in urls:
        return [_active_url] + [u for u in urls if u != _active_url]
    return list(urls)


async def push_tokens(session: dict) -> bool:
    """POST the current token set to selkies. Replaces the whole active set:
    removed tokens disconnect their clients, changed permissions apply live."""
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
    """Empty token set: every streaming client is disconnected."""
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
