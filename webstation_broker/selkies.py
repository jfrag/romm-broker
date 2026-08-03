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


async def push_tokens(session: dict) -> bool:
    """POST the current token set to selkies. Replaces the whole active set:
    removed tokens disconnect their clients, changed permissions apply live."""
    tokens = build_token_map(session)
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.post(
                f"{settings.SELKIES_CONTROL_URL}/tokens",
                json=tokens,
                headers={"Authorization": f"Bearer {settings.SELKIES_MASTER_TOKEN}"},
            )
            resp.raise_for_status()
        return True
    except Exception as exc:
        log.warning("selkies token push failed: %s", exc)
        return False


async def clear_tokens() -> bool:
    """Empty token set: every streaming client is disconnected."""
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.post(
                f"{settings.SELKIES_CONTROL_URL}/tokens",
                json={},
                headers={"Authorization": f"Bearer {settings.SELKIES_MASTER_TOKEN}"},
            )
            resp.raise_for_status()
        return True
    except Exception as exc:
        log.warning("selkies token clear failed: %s", exc)
        return False
