"""Exit-time save archive push to the parent (RomM).

The broker is normally served same-origin under the parent's SUBFOLDER, so
activate derives the callback base URL from the request that launched the
session; an explicit callback.base_url in the activate payload overrides it
for split-origin deployments.
"""

import logging

import httpx

from . import settings

log = logging.getLogger(__name__)


def public_view(callback: dict | None) -> dict | None:
    """Callback info safe to echo in reports: everything but the token."""
    if not callback:
        return None
    return {k: v for k, v in callback.items() if k != "token"}


async def push_save_archive(
    callback: dict, zip_bytes: bytes, filename: str, sess: dict
) -> dict:
    """POST the save archive to the callback origin as multipart form data.

    Returns {"mode": "uploaded"|"failed", "ok": bool, "url", ...}. Failures
    are reported, never raised: exit teardown must finish regardless.
    """
    url = callback["base_url"].rstrip("/") + settings.SAVE_UPLOAD_PATH
    headers = {}
    if callback.get("token"):
        headers["Authorization"] = f"Bearer {callback['token']}"
    rom = sess.get("rom") or {}
    data = {"session_id": sess["id"], "emulator": sess["emulator"]}
    if rom.get("id") is not None:
        data["rom_id"] = str(rom["id"])
    if rom.get("name"):
        data["rom_name"] = rom["name"]
    files = {"archive": (filename, zip_bytes, "application/zip")}
    try:
        async with httpx.AsyncClient(timeout=settings.SAVE_UPLOAD_TIMEOUT) as client:
            resp = await client.post(url, data=data, files=files, headers=headers)
            resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        log.warning("save upload rejected by %s: HTTP %d", url, exc.response.status_code)
        return {
            "mode": "failed",
            "ok": False,
            "url": url,
            "status_code": exc.response.status_code,
            "error": f"upload rejected: HTTP {exc.response.status_code}",
        }
    except Exception as exc:
        log.warning("save upload to %s failed: %s", url, exc)
        return {"mode": "failed", "ok": False, "url": url, "error": str(exc)}
    log.info("save upload: %d bytes to %s (HTTP %d)", len(zip_bytes), url, resp.status_code)
    return {"mode": "uploaded", "ok": True, "url": url, "status_code": resp.status_code}
