"""PCSX2 launcher: ROM folder→disc-image resolution, PCSX2.ini patching, and
PINE-driven save state load/save."""

import logging
import os
import re
import socket as _socket
import struct
import time
from pathlib import Path
from threading import Thread

from .base import Emulator, base_launch_env

log = logging.getLogger(__name__)

ROM_ROOT = Path(os.environ.get("ROM_ROOT", "/romm"))

INI_PATH = Path("/config/.config/PCSX2/inis/PCSX2.ini")
SSTATE_DIR = Path(os.environ.get("SSTATE_DIR", "/config/.config/PCSX2/sstates"))
PCSX2_LOG_PATH = Path(os.environ.get("PCSX2_LOG_PATH", "/config/pcsx2-qt.log"))

PINE_WAIT = float(os.environ.get("PINE_WAIT", "20.0"))
RESUME_LOAD_WAIT = float(os.environ.get("RESUME_LOAD_WAIT", "90.0"))
RESUME_LOAD_SETTLE = float(os.environ.get("RESUME_LOAD_SETTLE", "3.0"))

XDG_RUNTIME_DIR = os.environ.get("XDG_RUNTIME_DIR", "/config/.XDG")
PINE_SOCKET = Path(XDG_RUNTIME_DIR) / "pcsx2.sock"

_PINE_MSG_SAVE_STATE = 0x09
_PINE_MSG_LOAD_STATE = 0x0A
_PINE_MSG_EMU_STATUS = 0x0F


# Disc formats pcsx2-qt can boot, best first; a folder holding several
# candidates picks by this order so a .chd beats the raw .bin beside it.
ROM_EXTENSIONS = (".chd", ".iso", ".cso", ".zso", ".gz", ".mdf", ".dump", ".bin", ".elf")
_ROM_SEARCH_GLOBS = ("*", "*/*")
_DISC_RE = re.compile(r"(?:^|[^a-z0-9])(?:disc|disk|cd)[\s._-]*(\d+)", re.IGNORECASE)


def _disc_number(rel: Path) -> int:
    match = _DISC_RE.search(str(rel))
    if match is None:
        return 1
    return max(1, int(match.group(1)))


def _pick_rom_file(candidates, base: Path) -> Path | None:
    ranked = []
    for p in candidates:
        if p.name.startswith("."):
            continue
        ext = p.suffix.lower()
        if ext not in ROM_EXTENSIONS:
            continue
        try:
            if not p.is_file():
                continue
            real = p.resolve()
            rel = p.relative_to(base)
        except (OSError, ValueError):
            continue
        if not real.is_relative_to(ROM_ROOT):
            continue
        ranked.append(
            (_disc_number(rel), ROM_EXTENSIONS.index(ext), len(rel.parts), p.name.lower(), real)
        )
    if not ranked:
        return None
    return min(ranked)[4]


# Selkies exposes the browser gamepad as an SDL device, but PCSX2 only maps a
# controller through its setup wizard, which never runs here, so Pad1 would
# stay on the keyboard-only defaults.
_PAD1_SDL_BINDINGS = (
    ("Up", "DPadUp"),
    ("Right", "DPadRight"),
    ("Down", "DPadDown"),
    ("Left", "DPadLeft"),
    ("Triangle", "FaceNorth"),
    ("Circle", "FaceEast"),
    ("Cross", "FaceSouth"),
    ("Square", "FaceWest"),
    ("Select", "Back"),
    ("Start", "Start"),
    ("L1", "LeftShoulder"),
    ("L2", "+LeftTrigger"),
    ("R1", "RightShoulder"),
    ("R2", "+RightTrigger"),
    ("L3", "LeftStick"),
    ("R3", "RightStick"),
    ("LUp", "-LeftY"),
    ("LRight", "+LeftX"),
    ("LDown", "+LeftY"),
    ("LLeft", "-LeftX"),
    ("RUp", "-RightY"),
    ("RRight", "+RightX"),
    ("RDown", "+RightY"),
    ("RLeft", "-RightX"),
    ("Analog", "Guide"),
    ("LargeMotor", "LargeMotor"),
    ("SmallMotor", "SmallMotor"),
)


def _ensure_sdl_pad(lines: list[str]) -> list[str]:
    """Bind Pad1 to the virtual SDL pad, once. Repeated keys are how PCSX2
    itself stores a second binding per action, so the keyboard defaults keep
    working alongside the gamepad. Skipped when Pad1 already names an SDL
    device, so a player's own remapping survives the next launch."""
    bindings = [f"{action} = SDL-0/{binding}" for action, binding in _PAD1_SDL_BINDINGS]
    start = None
    end = len(lines)
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not (stripped.startswith("[") and stripped.endswith("]")):
            continue
        if stripped == "[Pad1]":
            start = i
        elif start is not None:
            end = i
            break
    if start is None:
        return lines + ["", "[Pad1]", "Type = DualShock2"] + bindings
    if any("SDL-0/" in ln for ln in lines[start:end]):
        return lines
    insert = end
    while insert > start + 1 and not lines[insert - 1].strip():
        insert -= 1
    return lines[:insert] + bindings + lines[insert:]


def _patch_ini() -> None:
    """Force broker-required PCSX2.ini settings before every launch.

    On a fresh container the file does not exist yet: PCSX2 writes it during
    its own first start, by which point it is already sitting on the setup
    wizard with PINE off. Seeding an empty file here means the patch below
    creates the sections it needs, so the very first launch boots the disc."""
    patches: dict[tuple[str, str], str] = {
        ("EmuCore", "EnablePINE"): "EnablePINE = true",
        ("UI", "StartFullscreen"): "StartFullscreen = true",
        ("UI", "ConfirmShutdown"): "ConfirmShutdown = false",
        ("UI", "SetupWizardIncomplete"): "SetupWizardIncomplete = false",
        ("EmuCore", "SaveStateOnShutdown"): "SaveStateOnShutdown = false",
    }
    try:
        if not INI_PATH.exists():
            log.info("PCSX2.ini not found at %s, seeding one", INI_PATH)
            INI_PATH.parent.mkdir(parents=True, exist_ok=True)
            INI_PATH.write_text("[UI]\nSettingsVersion = 1\n")
        lines = INI_PATH.read_text().splitlines()
        section = ""
        applied: set[tuple[str, str]] = set()
        new_lines: list[str] = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                section = stripped[1:-1]
                new_lines.append(line)
                continue
            matched = False
            for (sec, key), val in patches.items():
                if section != sec:
                    continue
                if stripped.startswith(f"{key} =") or stripped.startswith(f"{key}="):
                    new_lines.append(val)
                    applied.add((sec, key))
                    matched = True
                    break
            if not matched:
                new_lines.append(line)
        missing = [(s, k, v) for (s, k), v in patches.items() if (s, k) not in applied]
        if missing:
            present = {
                ln.strip()[1:-1]
                for ln in new_lines
                if ln.strip().startswith("[") and ln.strip().endswith("]")
            }
            for sec, _key, val in missing:
                if sec in present:
                    out: list[str] = []
                    inserted = False
                    for ln in new_lines:
                        out.append(ln)
                        if not inserted and ln.strip() == f"[{sec}]":
                            out.append(val)
                            inserted = True
                    new_lines = out
                else:
                    new_lines.extend(["", f"[{sec}]", val])
                    present.add(sec)
        new_lines = _ensure_sdl_pad(new_lines)
        tmp = INI_PATH.with_suffix(".tmp")
        tmp.write_text("\n".join(new_lines) + "\n")
        tmp.replace(INI_PATH)
    except Exception:
        log.exception("PCSX2.ini patch failed, broker settings NOT applied")


def _sstate_snapshot() -> dict:
    if not SSTATE_DIR.is_dir():
        return {}
    snap = {}
    for p in SSTATE_DIR.glob("*.p2s"):
        try:
            st = p.stat()
            snap[p] = (st.st_size, st.st_mtime)
        except OSError:
            pass
    return snap


def _matches_slot(p: Path, slot: int) -> bool:
    return p.name.endswith(f".{slot:02d}.p2s") or p.name.endswith(f".{slot}.p2s")


def _wait_for_sstate_write(before: dict, deadline: float, slot: int | None = None) -> bool:
    """Poll SSTATE_DIR until a slot-matching write completes (size stable for
    0.5 s) or the deadline passes. PCSX2 acks PINE saves before writing, so
    this is the only reliable confirmation."""
    STABLE_SECS = 0.5
    POLL_SECS = 0.1
    target: Path | None = None
    last_size: int | None = None
    stable_since: float | None = None
    while time.monotonic() < deadline:
        after = _sstate_snapshot()
        if target is None:
            for p, (size, mtime) in after.items():
                if slot is not None and not _matches_slot(p, slot):
                    continue
                prev = before.get(p)
                if prev is None or prev[1] != mtime:
                    target = p
                    last_size = size
                    stable_since = time.monotonic()
                    break
        else:
            cur = after.get(target)
            if cur is None:
                target = None
            else:
                if cur[0] != last_size:
                    last_size = cur[0]
                    stable_since = time.monotonic()
                elif time.monotonic() - stable_since >= STABLE_SECS:
                    log.info("save state write complete: %s (%d bytes)", target.name, last_size)
                    return True
        time.sleep(POLL_SECS)
    return False


def newest_state_for_slot(slot: int) -> Path | None:
    if not SSTATE_DIR.is_dir():
        return None
    candidates = []
    for pattern in {f"*.{slot:02d}.p2s", f"*.{slot}.p2s"}:
        for p in SSTATE_DIR.glob(pattern):
            try:
                candidates.append((p.stat().st_mtime, p))
            except OSError:
                pass
    if not candidates:
        return None
    return max(candidates)[1]


def _pine_recv_exact(sock: _socket.socket, n: int) -> bytes | None:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def _pine_request(opcode: int, payload: bytes = b"", timeout: float = 5.0) -> bytes | None:
    """One PINE request. Wire format (LE): u32 total size, u8 opcode, payload;
    reply is u32 size, u8 result (0 = OK), payload."""
    packet = struct.pack("<IB", 5 + len(payload), opcode) + payload
    try:
        with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(str(PINE_SOCKET))
            sock.sendall(packet)
            header = _pine_recv_exact(sock, 5)
            if header is None:
                return None
            size, result = struct.unpack("<IB", header)
            body = b""
            if size > 5:
                body = _pine_recv_exact(sock, size - 5) or b""
            if result != 0:
                log.warning("PINE opcode 0x%02X rejected (result %d)", opcode, result)
                return None
            return body
    except OSError as exc:
        log.warning("PINE request failed on %s (opcode 0x%02X): %s", PINE_SOCKET, opcode, exc)
        return None


def _pine_emu_status() -> int | None:
    """0 running, 1 paused, 2 shutdown, None if PINE is down."""
    body = _pine_request(_PINE_MSG_EMU_STATUS, timeout=2.0)
    if body is None or len(body) < 4:
        return None
    return struct.unpack("<I", body[:4])[0]


class Pcsx2(Emulator):
    name = "pcsx2"
    display_name = "PCSX2"
    save_root = Path("/config/.config/PCSX2")
    save_subtrees = ("memcards", "sstates")
    rom_extensions = ROM_EXTENSIONS
    log_path = PCSX2_LOG_PATH

    def __init__(self):
        super().__init__()
        self._launch_seq = 0

    def resolve_rom_file(self, path: Path) -> Path | None:
        if path.is_file():
            return path
        if not path.is_dir():
            return None
        candidates: list[Path] = []
        for pattern in _ROM_SEARCH_GLOBS:
            try:
                candidates.extend(path.glob(pattern))
            except OSError:
                return None
        return _pick_rom_file(candidates, path)

    def launch(self, rom_path: Path, resume_slot: int | None) -> None:
        self.stop()
        _patch_ini()
        self._launch_seq += 1
        seq = self._launch_seq

        binary = os.environ.get("PCSX2_BIN", "pcsx2-qt")
        log.info("launching pcsx2 (rom=%s, resume_slot=%s)", rom_path, resume_slot)
        self._spawn(
            [binary, "-batch", "-fullscreen", "--", str(rom_path)], base_launch_env()
        )

        if resume_slot:
            Thread(
                target=self._deferred_load_state, args=(resume_slot, seq), daemon=True
            ).start()

    def _deferred_load_state(self, slot: int, seq: int) -> None:
        """Load `slot` once the freshly launched game's VM reports running."""
        deadline = time.monotonic() + RESUME_LOAD_WAIT
        while time.monotonic() < deadline:
            if self._launch_seq != seq:
                log.info("resume: launch superseded, slot %d load abandoned", slot)
                return
            if _pine_emu_status() == 0:
                time.sleep(RESUME_LOAD_SETTLE)
                if self._launch_seq != seq:
                    return
                ok = _pine_request(_PINE_MSG_LOAD_STATE, bytes([slot])) is not None
                log.info("resume: deferred load of slot %d %s", slot, "delivered" if ok else "failed")
                return
            time.sleep(1.0)
        log.warning("resume: VM never reached running state, slot %d not loaded", slot)

    def save_state(self, slot: int) -> bool:
        before = _sstate_snapshot()
        if _pine_request(_PINE_MSG_SAVE_STATE, bytes([slot])) is None:
            return False
        return _wait_for_sstate_write(before, time.monotonic() + PINE_WAIT, slot)

    def save_and_exit(self, slot: int) -> dict:
        saved = False
        state_file = None
        if self.alive():
            saved = self.save_state(slot)
            if saved:
                p = newest_state_for_slot(slot)
                if p is not None:
                    st = p.stat()
                    state_file = {"path": str(p), "size": st.st_size, "mtime": st.st_mtime}
        self.stop()
        return {"state_saved": saved, "state_slot": slot, "state_file": state_file}

    def stop(self) -> None:
        # Invalidate any in-flight deferred state load before the kill.
        self._launch_seq += 1
        super().stop()
