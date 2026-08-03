"""REST endpoints: session lifecycle (activate / join / exit / status) plus
the context endpoint the room frontend bootstraps from. All endpoints live
under the SUBFOLDER prefix; the RomM-facing ones require X-Broker-Secret when
BROKER_SECRET is set."""

import hmac
import logging
import time
from pathlib import Path
from typing import Optional

import anyio
from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel

from . import callback, saves, selkies, session, settings
from .emulators import get_emulator

log = logging.getLogger(__name__)
router = APIRouter()


class SaveIn(BaseModel):
    archive: Optional[str] = None
    resume_slot: Optional[int] = None


class RomIn(BaseModel):
    id: Optional[int] = None
    name: Optional[str] = None
    platform: Optional[str] = None
    path: str


class CallbackIn(BaseModel):
    base_url: Optional[str] = None
    token: Optional[str] = None


class JoinIn(BaseModel):
    user: Optional[dict] = None
    permission: str = "participant"


class ActivateIn(BaseModel):
    session_id: Optional[str] = None
    user: Optional[dict] = None
    emulator: str
    # Optional for launch types without a game (e.g. emulator "desktop").
    rom: Optional[RomIn] = None
    save: Optional[SaveIn] = None
    callback: Optional[CallbackIn] = None


def _ct_eq(a: str, b: str) -> bool:
    """Constant-time compare tolerant of non-ASCII input."""
    return hmac.compare_digest(
        a.encode("utf-8", "replace"), b.encode("utf-8", "replace")
    )


def _check_secret(header_value: Optional[str]) -> None:
    if not settings.BROKER_SECRET:
        return
    if not header_value or not _ct_eq(header_value, settings.BROKER_SECRET):
        raise HTTPException(status_code=403, detail="bad broker secret")


def _landing_url(token: str) -> str:
    return f"{settings.PREFIX}/?token={token}"


def _resolve_callback(body_cb: Optional[CallbackIn], request: Request) -> dict:
    """Where the exit save archive goes. Same-origin deployments don't need
    to send a base_url: the broker sits under the parent's SUBFOLDER, so the
    origin that served the activate request IS the parent. An explicit
    base_url still wins for split-origin setups."""
    if body_cb and body_cb.base_url:
        return {"base_url": body_cb.base_url, "token": body_cb.token, "derived": False}
    proto = (
        request.headers.get("x-forwarded-proto") or request.url.scheme
    ).split(",")[0].strip()
    host = (
        request.headers.get("x-forwarded-host")
        or request.headers.get("host")
        or request.url.netloc
    ).split(",")[0].strip()
    return {
        "base_url": f"{proto}://{host}",
        "token": body_cb.token if body_cb else None,
        "derived": True,
    }


@router.get("/api/health")
async def health():
    return {"status": "ok"}


@router.post("/api/session/activate")
async def activate(
    body: ActivateIn,
    request: Request,
    x_broker_secret: Optional[str] = Header(default=None),
):
    _check_secret(x_broker_secret)

    if session.SESSION is not None and session.SESSION.get("active"):
        raise HTTPException(
            status_code=409,
            detail="a session is already active; exit it before activating a new one",
        )

    emulator = get_emulator(body.emulator)
    if emulator is None:
        raise HTTPException(status_code=422, detail=f"unknown emulator: {body.emulator}")

    rom_file = None
    if emulator.requires_rom:
        if body.rom is None:
            raise HTTPException(
                status_code=422, detail=f"emulator {body.emulator} requires a rom"
            )
        try:
            rom_path = Path(body.rom.path).resolve()
        except OSError:
            raise HTTPException(status_code=400, detail="invalid rom path")
        if not rom_path.is_relative_to(settings.ROM_ROOT):
            raise HTTPException(
                status_code=400, detail=f"rom path must live under {settings.ROM_ROOT}"
            )
        if not rom_path.exists():
            raise HTTPException(status_code=404, detail="rom path does not exist")
        rom_file = emulator.resolve_rom_file(rom_path)
        if rom_file is None:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "no bootable file found",
                    "extensions": list(emulator.rom_extensions),
                },
            )

    # Restore save data before the emulator boots.
    restore_report = None
    save = body.save
    if save and save.archive and emulator.save_subtrees:
        archive_path = Path(save.archive)
        if not archive_path.is_file():
            raise HTTPException(
                status_code=404, detail=f"save archive not found: {save.archive}"
            )
        content = await anyio.to_thread.run_sync(archive_path.read_bytes)
        restore_report = await anyio.to_thread.run_sync(
            saves.extract_save_archive, content, emulator.save_root, emulator.save_subtrees
        )
        if restore_report["error"]:
            raise HTTPException(
                status_code=422, detail=f"save restore failed: {restore_report['error']}"
            )
        log.info("save restore: %s", restore_report)

    payload = body.model_dump()
    payload["callback"] = _resolve_callback(body.callback, request)
    sess = session.new_session(
        payload, emulator, str(rom_file) if rom_file else None
    )

    resume_slot = save.resume_slot if save else None
    await anyio.to_thread.run_sync(emulator.launch, rom_file, resume_slot)
    # Baseline after launch: the exit dump only ships files this session wrote.
    sess["save_baseline"] = time.time()

    tokens_pushed = await selkies.push_tokens(sess)

    return {
        "status": "launching",
        "session_id": sess["id"],
        "rom_file": str(rom_file) if rom_file else None,
        "save_restore": restore_report,
        "selkies_tokens_pushed": tokens_pushed,
        "url": _landing_url(sess["controller_token"]),
    }


@router.post("/api/session/join")
async def join(body: JoinIn, x_broker_secret: Optional[str] = Header(default=None)):
    """Add a user to the running session. Membership policy belongs to the
    caller; whoever is sent gets a personal token and landing URL."""
    _check_secret(x_broker_secret)

    sess = session.SESSION
    if sess is None or not sess.get("active"):
        raise HTTPException(status_code=409, detail="no active session to join")

    if body.permission not in ("participant", "readonly"):
        raise HTTPException(
            status_code=422, detail="permission must be participant or readonly"
        )

    viewer = session.add_viewer(body.permission, body.user)
    await selkies.push_tokens(sess)
    await session.broadcast_state()

    return {
        "status": "joined",
        "session_id": sess["id"],
        "permission": body.permission,
        "username": viewer["username"],
        "url": _landing_url(viewer["token"]),
    }


async def _do_exit(save_slot: int) -> dict:
    """Save state, stop the emulator, dump the save delta, and report."""
    sess = session.SESSION
    if sess is None or not sess.get("active"):
        raise HTTPException(status_code=409, detail="no active session")

    emulator = sess["emulator_obj"]
    exit_report = await anyio.to_thread.run_sync(emulator.save_and_exit, save_slot)

    dump = await anyio.to_thread.run_sync(
        saves.build_save_archive,
        emulator.save_root,
        emulator.save_subtrees,
        sess["save_baseline"],
    )
    cb = sess.get("callback")
    archive_name = f"{sess['id']}-{int(time.time())}.zip"
    archive_path = None
    if settings.DEV_MODE:
        if dump.get("zip_bytes"):
            archive_path = await anyio.to_thread.run_sync(
                saves.write_export, dump["zip_bytes"], archive_name
            )
        upload = {
            "mode": "report-only",
            "note": "dev mode: nothing was uploaded",
            "callback": callback.public_view(cb),
            "would_send": {
                "files": len(dump["files"]),
                "total_bytes": dump["total_bytes"],
                "archive_path": archive_path,
            },
        }
    elif not dump.get("zip_bytes"):
        upload = {"mode": "skipped", "ok": True, "note": "no save changes to upload"}
    else:
        upload = await callback.push_save_archive(
            cb, dump["zip_bytes"], archive_name, sess
        )
        if not upload["ok"]:
            # Keep the archive on disk so a failed push never loses save data.
            archive_path = await anyio.to_thread.run_sync(
                saves.write_export, dump["zip_bytes"], archive_name
            )
            upload["archive_path"] = archive_path

    report = {
        "status": "exited",
        "session_id": sess["id"],
        "rom": sess.get("rom"),
        **exit_report,
        "save_dump": {
            "files": dump["files"],
            "skipped": dump["skipped"],
            "total_bytes": dump["total_bytes"],
            "archive_path": archive_path,
            "error": dump["error"],
        },
        "upload": upload,
    }

    if settings.DEV_MODE:
        outcome = f"would upload to {cb['base_url']} (dev mode)"
    elif upload["mode"] == "uploaded":
        outcome = f"uploaded to {upload['url']}"
    elif upload["mode"] == "skipped":
        outcome = "nothing to upload"
    else:
        outcome = f"upload failed ({upload.get('error')}), archive kept at {archive_path}"
    summary = (
        f"Session ended. Dumped {len(dump['files'])} save file(s), "
        f"{dump['total_bytes']} bytes"
        + (f" → {archive_path}" if archive_path else "")
        + f"; {outcome}"
        + f". State saved: {exit_report.get('state_saved')}."
    )
    await session.broadcast_to_room(
        {
            "type": "chat_message",
            "sender": "Broker",
            "message": summary,
            "timestamp": int(time.time() * 1000),
            "messageId": f"broker-{int(time.time() * 1000)}",
        }
    )
    await session.notify_session_ended()
    # Give browsers time to tear the stream iframe down before the token set
    # empties; selkies errors on connected clients whose token vanished.
    await anyio.sleep(1.5)
    await selkies.clear_tokens()
    session.clear_session()
    log.info("exit report: %s", report)
    return report


@router.post("/api/session/exit")
async def exit_session(
    request: Request,
    x_broker_secret: Optional[str] = Header(default=None),
    token: Optional[str] = Query(default=None),
    slot: int = Query(default=10, ge=1, le=10),
):
    """Accepts either the broker secret or the controller token."""
    sess = session.SESSION
    is_controller = bool(sess and token and _ct_eq(token, sess["controller_token"]))
    if not is_controller:
        _check_secret(x_broker_secret)
    return await _do_exit(slot)


@router.get("/api/session/status")
async def status():
    sess = session.SESSION
    if sess is None:
        return {"active": False}
    return {
        "active": sess.get("active", False),
        "session_id": sess["id"],
        "emulator": sess["emulator"],
        "rom": sess.get("rom"),
        "rom_file": sess.get("rom_file"),
        "emulator_alive": sess["emulator_obj"].alive(),
        "started_at": sess.get("created_at"),
        "user": sess.get("user"),
        "viewers": [
            {"username": v.get("username"), "user_id": v.get("user_id"),
             "permission": v.get("permission")}
            for v in sess.get("viewers", [])
        ],
    }


@router.get("/api/session/context")
async def context(token: Optional[str] = Query(default=None)):
    """Frontend bootstrap: resolve an arriving token into the caller's role."""
    sess = session.SESSION
    if sess is None or not sess.get("active"):
        raise HTTPException(status_code=409, detail="no active session")
    if not token:
        raise HTTPException(status_code=401, detail="missing token")

    role = None
    permission = "participant"
    username = None

    if _ct_eq(token, sess["controller_token"]):
        role = "controller"
        username = (sess.get("user") or {}).get("display_name") or (
            sess.get("user") or {}
        ).get("username")
    else:
        viewer = session.find_viewer(token)
        if viewer is not None:
            role = "viewer"
            permission = viewer.get("permission", "participant")
            username = viewer.get("username")

    if role is None:
        raise HTTPException(status_code=401, detail="invalid token")

    return {
        "sessionId": sess["id"],
        "userRole": role,
        "userToken": token,
        "userPermission": permission if role == "viewer" else "participant",
        "username": username,
        "gameName": (sess.get("rom") or {}).get("name")
        or sess["emulator_obj"].display_name,
        "controllerName": (sess.get("user") or {}).get("display_name") or "Controller",
        # Relative to the page base; the selkies client reads the token from
        # its query string.
        "iframeSrc": f"stream/?token={token}",
    }
