"""REST endpoints: session lifecycle (activate / join / exit / status) plus
the context endpoint the room frontend bootstraps from. All endpoints live
under the SUBFOLDER prefix; the RomM-facing ones require X-Broker-Secret when
BROKER_SECRET is set."""

import hmac
import logging
import os
import time
from pathlib import Path
from typing import Optional

import anyio
from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

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


class StateIn(BaseModel):
    # RomM is the library of states, so the emulator works in a single slot and
    # resolves whatever is asked for to it. The bound is kept only to reject
    # obvious garbage, and 0 is the other brokers' "use your default slot".
    slot: int = Field(default=0, ge=0, le=10)


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
    # General-purpose emulators (retroarch) pick their core from the platform.
    if body.rom is not None:
        emulator.platform = body.rom.platform

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

    # Restore save data before the emulator boots. The working slot is emptied
    # first so the restore, not the previous session, decides what is in it.
    await anyio.to_thread.run_sync(emulator.clear_working_slot)
    restore_report = None
    save = body.save
    if save and save.archive and emulator.save_subtrees:
        archive_path = Path(save.archive)
        if not archive_path.is_file():
            raise HTTPException(
                status_code=404, detail=f"save archive not found: {save.archive}"
            )
        content = await anyio.to_thread.run_sync(archive_path.read_bytes)
        await anyio.to_thread.run_sync(emulator.prepare_restore)
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
            archive_path = await anyio.to_thread.run_sync(saves.write_export, dump["zip_bytes"], archive_name)
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
            archive_path = await anyio.to_thread.run_sync(saves.write_export, dump["zip_bytes"], archive_name)
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
    session.retire_session()
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


def _state_emulator():
    """The running emulator, if it is in a position to take a state command."""
    sess = session.SESSION
    if sess is None or not sess.get("active"):
        raise HTTPException(status_code=409, detail="no active session")
    emulator = sess["emulator_obj"]
    if not emulator.supports_states:
        raise HTTPException(
            status_code=400,
            detail=f"{emulator.display_name} has no save states",
        )
    if not emulator.alive():
        raise HTTPException(status_code=409, detail="emulator is not running")
    return emulator


def _readable_emulator():
    """The emulator whose state files can be read right now.

    The live session's while one is up, otherwise the one that just exited.
    Reading a state needs the file on disk and the emulator's naming rules, not
    a running process, and the exit state is the one RomM comes back for.
    """
    sess = session.SESSION
    if sess is not None and sess.get("active"):
        return sess["emulator_obj"]
    retired = session.LAST_EXIT
    if retired is None:
        raise HTTPException(status_code=409, detail="no session to read a state from")
    return retired["emulator_obj"]


@router.post("/api/session/save-state")
async def save_state(body: StateIn, x_broker_secret: Optional[str] = Header(default=None)):
    _check_secret(x_broker_secret)
    emulator = _state_emulator()
    saved = await anyio.to_thread.run_sync(emulator.save_state, body.slot)
    slot = emulator.state_slot
    log.info("save state slot %d: %s", slot, "ok" if saved else "failed")
    return {"status": "saved" if saved else "failed", "slot": slot, "saved": saved}


@router.post("/api/session/load-state")
async def load_state(body: StateIn, x_broker_secret: Optional[str] = Header(default=None)):
    _check_secret(x_broker_secret)
    emulator = _state_emulator()
    loaded = await anyio.to_thread.run_sync(emulator.load_state, body.slot)
    slot = emulator.state_slot
    log.info("load state slot %d: %s", slot, "ok" if loaded else "failed")
    return {
        "status": "loaded" if loaded else "failed",
        "slot": slot,
        "loaded": loaded,
    }


def _header_token(value: str, fallback: str) -> str:
    """`value` reduced to something safe to put in a response header.

    A Linux filename may hold CR, LF and any non-ASCII byte, all of which would
    either split the response or fail the header encoder, so anything outside
    printable ASCII is dropped rather than passed through."""
    cleaned = "".join(c for c in value if 32 <= ord(c) < 127)
    return cleaned or fallback


@router.get("/api/session/state-file")
async def get_state_file(x_broker_secret: Optional[str] = Header(default=None)):
    """Serve the working slot's state file so RomM can file it in the library.

    The slot is the emulator's own, not the caller's: `slot` is accepted for
    symmetry with the per-emulator brokers and ignored the same way the save
    routes ignore it. Served after exit as well as during the session, because
    the state exit captures is exactly the one RomM comes back for once the
    teardown has answered."""
    _check_secret(x_broker_secret)
    emulator = _readable_emulator()
    path = await anyio.to_thread.run_sync(emulator.state_path)
    if path is None:
        raise HTTPException(status_code=404, detail="no state file for slot")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"could not read state file: {exc}")
    if size > settings.STATE_FILE_MAX_BYTES:
        raise HTTPException(status_code=413, detail="state file exceeds size limit")
    log.info("state-file: serving %s (%d bytes)", path.name, size)
    return FileResponse(
        path,
        media_type="application/octet-stream",
        headers={
            "X-State-Filename": _header_token(path.name, "state"),
            "X-State-Slot": str(emulator.state_slot),
        },
    )


@router.get("/api/session/state-screenshot")
async def get_state_screenshot(x_broker_secret: Optional[str] = Header(default=None)):
    """Serve the frame captured with the working slot's state.

    Only for emulators that write the thumbnail as its own file; the ones that
    embed it in the state answer 404, which is the caller's cue to read the
    frame out of the state it already fetched. `slot` is accepted and ignored
    the same way the state-file routes ignore it, and like the state itself the
    frame stays readable after exit."""
    _check_secret(x_broker_secret)
    emulator = _readable_emulator()
    path = await anyio.to_thread.run_sync(emulator.state_screenshot_path)
    if path is None:
        raise HTTPException(status_code=404, detail="no state screenshot for slot")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"could not read screenshot: {exc}")
    if size > settings.STATE_SCREENSHOT_MAX_BYTES:
        raise HTTPException(status_code=413, detail="screenshot exceeds size limit")
    log.info("state-screenshot: serving %s (%d bytes)", path.name, size)
    return FileResponse(path, media_type="image/png")


@router.put("/api/session/state-file")
async def put_state_file(
    request: Request,
    filename: str = Query(...),
    x_broker_secret: Optional[str] = Header(default=None),
):
    """Write a state RomM is sending back into the working slot.

    The name has to be one the running emulator could have written for the
    loaded game, which is what state_target decides; anything else is refused
    rather than dropped somewhere in the save tree under a name nothing reads.
    The slot it was captured in does not have to match, since RomM holds the
    library, so the reply reports the name it was filed under."""
    _check_secret(x_broker_secret)
    emulator = _state_emulator()
    name = Path(filename).name
    target = emulator.state_target(name)
    if target is None:
        raise HTTPException(status_code=400, detail="filename is not a state this emulator would write")

    tmp = target.with_name(f".{target.name}.tmp")
    written = 0
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # Streamed to disk rather than buffered: a state runs to hundreds of
        # megabytes and holding one per request on the heap is the whole limit
        # multiplied by however many callers are pushing at once.
        with tmp.open("wb") as out:
            async for chunk in request.stream():
                written += len(chunk)
                if written > settings.STATE_FILE_MAX_BYTES:
                    raise HTTPException(status_code=413, detail="state file exceeds size limit")
                await anyio.to_thread.run_sync(out.write, chunk)
        if written == 0:
            raise HTTPException(status_code=400, detail="empty request body")
        os.replace(tmp, target)
    except HTTPException:
        tmp.unlink(missing_ok=True)
        raise
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"could not write state file: {exc}")

    log.info("state-file: stored %s (%d bytes)", target.name, written)
    return {"status": "ok", "filename": target.name, "slot": emulator.state_slot}


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
        # The emulator class is the authority on what it can do, so RomM reads
        # this rather than keeping its own per-emulator table.
        "supports_states": sess["emulator_obj"].supports_states,
        # The slot every state route resolves to. There is only one because
        # RomM holds the library of states.
        "state_slot": sess["emulator_obj"].state_slot,
        "started_at": sess.get("created_at"),
        "user": sess.get("user"),
        "viewers": [
            {"username": v.get("username"), "user_id": v.get("user_id"),
             "permission": v.get("permission")}
            for v in sess.get("viewers", [])
        ],
    }


def _archive_name(name: str) -> str:
    """A safe zip basename, or 400. Rejects anything with path structure."""
    safe = Path(name).name
    if safe != name or not safe.endswith(".zip") or safe.startswith("."):
        raise HTTPException(status_code=400, detail="invalid archive name")
    return safe


def _export_file(name: str) -> Path:
    """Resolve an archive name to a file inside EXPORT_DIR."""
    candidate = (settings.EXPORT_DIR / _archive_name(name)).resolve()
    if candidate.parent != settings.EXPORT_DIR.resolve():
        raise HTTPException(status_code=400, detail="invalid export name")
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail=f"no such export: {name}")
    return candidate


@router.put("/api/session/imports/{name}")
async def upload_import(
    name: str, request: Request, x_broker_secret: Optional[str] = Header(default=None)
):
    """Take a save archive from the parent and return the path activate wants.

    Activate's save.archive is a container path, but the parent is a separate
    service with only bytes, so it uploads here first and passes back the
    path this returns. Body is the raw zip, matching the exit download.
    """
    _check_secret(x_broker_secret)
    safe = _archive_name(name)

    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > saves.SAVE_FILE_MAX_BYTES:
            raise HTTPException(status_code=413, detail="archive too large")
        chunks.append(chunk)
    content = b"".join(chunks)
    if not content.startswith(b"PK"):
        raise HTTPException(status_code=422, detail="archive is not a zip")

    path = await anyio.to_thread.run_sync(saves.write_import, content, safe)
    log.info("import stored: %s (%d bytes)", safe, total)
    return {"status": "stored", "name": safe, "path": path, "size": total}


@router.get("/api/session/exports")
async def list_exports(x_broker_secret: Optional[str] = Header(default=None)):
    """Save archives sitting on disk, newest first."""
    _check_secret(x_broker_secret)
    if not settings.EXPORT_DIR.is_dir():
        return {"exports": []}
    items = []
    for p in settings.EXPORT_DIR.glob("*.zip"):
        try:
            st = p.stat()
        except OSError:
            continue
        items.append({"name": p.name, "size": st.st_size, "mtime": st.st_mtime})
    items.sort(key=lambda i: i["mtime"], reverse=True)
    return {"exports": items}


@router.get("/api/session/exports/{name}")
async def download_export(
    name: str, x_broker_secret: Optional[str] = Header(default=None)
):
    """Hand an archive to the parent on request.

    Pulling covers the two cases the exit push cannot: dev mode, where the
    upload is disabled and the archive only ever lands here, and a failed
    upload, where it is the sole remaining copy of the save data.
    """
    _check_secret(x_broker_secret)
    path = _export_file(name)
    return FileResponse(path, media_type="application/zip", filename=path.name)


@router.delete("/api/session/exports/{name}")
async def delete_export(
    name: str, x_broker_secret: Optional[str] = Header(default=None)
):
    """Drop an archive once the parent has stored it."""
    _check_secret(x_broker_secret)
    path = _export_file(name)
    await anyio.to_thread.run_sync(path.unlink)
    log.info("export collected and removed: %s", path.name)
    return {"status": "deleted", "name": path.name}


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
