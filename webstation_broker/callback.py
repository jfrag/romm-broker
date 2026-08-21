"""Exit-time save archive push to the parent (RomM).

The broker is normally served same-origin under the parent's `SUBFOLDER`, so
activate derives the callback base URL from the request that launched the
session; an explicit `callback.base_url` in the activate payload overrides it
for split-origin deployments.
"""

import logging
from typing import Any

import httpx

from . import settings

log = logging.getLogger(__name__)


def public_view(callback: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return the callback info that is safe to echo in reports: everything but the token.

    Args:
        callback: The session's callback dict, or None when the session has none.

    Returns:
        A copy of `callback` without its `token` key, or None when `callback` is
        empty or None.
    """
    if not callback:
        return None
    return {k: v for k, v in callback.items() if k != "token"}


async def push_save_archive(
    callback: dict[str, Any], zip_bytes: bytes, filename: str, sess: dict[str, Any]
) -> dict[str, Any]:
    """POST the save archive to the callback origin as multipart form data.

    Failures are reported, never raised: exit teardown must finish regardless.

    Args:
        callback: The session's callback dict; `base_url` is required and
            `token`, when present, is sent as a bearer token.
        zip_bytes: The archive body.
        filename: The filename to attach to the multipart `archive` field.
        sess: The session the archive belongs to; its id, emulator and rom are
            sent as form fields alongside the archive.

    Returns:
        A report of the shape `{"mode": "uploaded" | "failed", "ok": bool, "url": str, ...}`,
        carrying `status_code` when the server answered and `error` when the
        upload failed.
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
