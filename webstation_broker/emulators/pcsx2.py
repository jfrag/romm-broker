"""PCSX2 launcher: disc-image resolution, PCSX2.ini patching, and PINE-driven save states.

Resolves a RomM ROM folder to the disc image pcsx2-qt boots, forces the
broker-required PCSX2.ini settings before every launch, keeps a folder memory
card waiting in Slot 1, and drives save state load and save over PCSX2's PINE
socket. A boot watchdog confirms the VM actually reaches a running state,
because a disc that fails to boot leaves PCSX2 sitting on an error dialog
rather than exiting.
"""

import logging
import os
import re
import socket as _socket
import struct
import time
from collections.abc import Iterable
from pathlib import Path
from threading import Thread
from typing import Any, Optional

from .. import memcard
from .base import Emulator, base_launch_env

log = logging.getLogger(__name__)

ROM_ROOT = Path(os.environ.get("ROM_ROOT", "/romm"))
"""Root of the RomM library mount (env `ROM_ROOT`, default `/romm`).

A resolved disc image must sit under it; candidates resolving outside are discarded.
"""

INI_PATH = Path("/config/.config/PCSX2/inis/PCSX2.ini")
"""The PCSX2.ini the broker patches before every launch."""
SSTATE_DIR = Path(os.environ.get("SSTATE_DIR", "/config/.config/PCSX2/sstates"))
"""Directory PCSX2 writes its `.p2s` save states into (env `SSTATE_DIR`)."""
PCSX2_LOG_PATH = Path(os.environ.get("PCSX2_LOG_PATH", "/config/pcsx2-qt.log"))
"""Log file the broker tails for this emulator (env `PCSX2_LOG_PATH`, default `/config/pcsx2-qt.log`)."""
STATE_SLOT = int(os.environ.get("PCSX2_STATE_SLOT", "10"))
"""The one slot the broker works in (env `PCSX2_STATE_SLOT`, default 10).

10 is PCSX2's own autosave slot, which the per-emulator broker has always
used, so containers that ran that one keep resolving their existing states.
"""

MEMCARD_DIR = Path("/config/.config/PCSX2/memcards")
"""Directory PCSX2 keeps its memory cards in."""
SLOT1_CARD_NAME = os.environ.get("PCSX2_SLOT1_CARD", "romm-slot1")
"""Name of the Slot-1 card the broker owns (env `PCSX2_SLOT1_CARD`, default `romm-slot1`).

PCSX2 tells a folder card from a file card by what it finds at the path, so
this is a directory and the name carries no `.ps2` extension, to keep it from
reading as one of PCSX2's own file cards.
"""
SLOT1_MARKER = "_pcsx2_superblock"
"""File that marks a directory as a folder card.

PCSX2 only counts a directory as a folder card once this file is inside it,
and skips the directory entirely otherwise, which leaves the slot reading as
missing with nowhere for the game to save.
"""

PINE_WAIT = float(os.environ.get("PINE_WAIT", "20.0"))
"""Seconds a save state has to land on disk after the PINE save command (env `PINE_WAIT`, default 20)."""
RESUME_LOAD_WAIT = float(os.environ.get("RESUME_LOAD_WAIT", "90.0"))
"""Seconds the boot watchdog gives the VM to start running (env `RESUME_LOAD_WAIT`, default 90)."""
RESUME_LOAD_SETTLE = float(os.environ.get("RESUME_LOAD_SETTLE", "3.0"))
"""Seconds to wait after the VM reports running before a resume load is sent (env `RESUME_LOAD_SETTLE`)."""

XDG_RUNTIME_DIR = os.environ.get("XDG_RUNTIME_DIR", "/config/.XDG")
"""Runtime directory PCSX2 creates its PINE socket in (env `XDG_RUNTIME_DIR`, default `/config/.XDG`)."""
PINE_SOCKET = Path(XDG_RUNTIME_DIR) / "pcsx2.sock"
"""Path of the PINE Unix socket pcsx2-qt listens on."""

_PINE_MSG_SAVE_STATE = 0x09
_PINE_MSG_LOAD_STATE = 0x0A
_PINE_MSG_EMU_STATUS = 0x0F


ROM_EXTENSIONS = (".chd", ".iso", ".cso", ".zso", ".gz", ".mdf", ".dump", ".bin", ".elf")
"""Disc formats pcsx2-qt can boot, best first.

A folder holding several candidates picks by this order so a `.chd` beats the
raw `.bin` beside it.
"""
_ROM_SEARCH_GLOBS = ("*", "*/*")
_DISC_RE = re.compile(r"(?:^|[^a-z0-9])(?:disc|disk|cd)[\s._-]*(\d+)", re.IGNORECASE)


def _disc_number(rel: Path) -> int:
    """Return the disc number a relative ROM path names, or 1 when it names none.

    Args:
        rel: Candidate path relative to the ROM folder being searched.

    Returns:
        The number following a `disc`, `disk` or `cd` marker in the path, never below 1.
    """
    match = _DISC_RE.search(str(rel))
    if match is None:
        return 1
    return max(1, int(match.group(1)))


def _pick_rom_file(candidates: Iterable[Path], base: Path) -> Optional[Path]:
    """Pick the best bootable disc image out of a set of candidate paths.

    Hidden files, unsupported extensions, non-files and anything resolving
    outside `ROM_ROOT` are dropped. The rest rank by disc number, then by
    position in `ROM_EXTENSIONS`, then by depth and name, so disc 1 in the
    best format wins.

    Args:
        candidates: Paths found under the ROM folder.
        base: The ROM folder the candidates are relative to.

    Returns:
        The resolved path of the winning image, or None when nothing qualifies.
    """
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
"""Pad1 action to SDL binding pairs for the virtual gamepad.

Selkies exposes the browser gamepad as an SDL device, but PCSX2 only maps a
controller through its setup wizard, which never runs here, so Pad1 would
stay on the keyboard-only defaults.
"""


def _ensure_sdl_pad(lines: list[str]) -> list[str]:
    """Bind Pad1 to the virtual SDL pad, once.

    Repeated keys are how PCSX2 itself stores a second binding per action, so
    the keyboard defaults keep working alongside the gamepad. Skipped when
    Pad1 already names an SDL device, so a player's own remapping survives
    the next launch.

    Args:
        lines: The PCSX2.ini contents, one entry per line.

    Returns:
        The same lines with the SDL bindings appended to the `[Pad1]` section, or the section
        created at the end when the file has none.
    """
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
    creates the sections it needs, so the very first launch boots the disc.
    Existing keys are rewritten in place, missing ones are added under their
    section (created when absent), the SDL pad is bound, and the result is
    written through a temp file. Any failure is logged rather than raised so
    the launch still goes ahead.
    """
    patches: dict[tuple[str, str], str] = {
        ("EmuCore", "EnablePINE"): "EnablePINE = true",
        ("UI", "StartFullscreen"): "StartFullscreen = true",
        ("UI", "ConfirmShutdown"): "ConfirmShutdown = false",
        ("UI", "SetupWizardIncomplete"): "SetupWizardIncomplete = false",
        ("EmuCore", "SaveStateOnShutdown"): "SaveStateOnShutdown = false",
        ("MemoryCards", "Slot1_Enable"): "Slot1_Enable = true",
        ("MemoryCards", "Slot1_Filename"): f"Slot1_Filename = {SLOT1_CARD_NAME}",
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


def _sstate_snapshot() -> dict[Path, tuple[int, float]]:
    """Snapshot every `.p2s` state in `SSTATE_DIR`.

    Returns:
        A dict of state path to `(size, mtime)`, empty when the directory is missing. Files that
        vanish mid-scan are skipped.
    """
    if not SSTATE_DIR.is_dir():
        return {}
    snap: dict[Path, tuple[int, float]] = {}
    for p in SSTATE_DIR.glob("*.p2s"):
        try:
            st = p.stat()
            snap[p] = (st.st_size, st.st_mtime)
        except OSError:
            pass
    return snap


def _matches_slot(p: Path, slot: int) -> bool:
    """Tell whether a state file belongs to `slot`, in either the padded or unpadded spelling.

    Args:
        p: A state file path.
        slot: The slot number to test against.

    Returns:
        True when the name ends in `.<slot>.p2s` with or without zero padding.
    """
    return p.name.endswith(f".{slot:02d}.p2s") or p.name.endswith(f".{slot}.p2s")


_STATE_NAME_RE = re.compile(r"^(?P<serial>.+)\.\d{1,2}\.p2s$")
"""Matches `<serial> (<crc>).<slot>.p2s`, the name PCSX2 builds for a save state.

The serial is what ties the file to a disc, the slot is just which of the ten
it went in.
"""


def _restamp_slot(filename: str, slot: int) -> Optional[str]:
    """Rename a state for `slot`, keeping the serial that ties it to its disc.

    RomM holds the library, so a stored state carries whatever slot it happened
    to be captured in. Only the serial has to survive the trip: PCSX2 looks a
    state up by the serial it reads off the running disc, so moving the name
    into this broker's one slot is what makes any stored capture loadable.

    Args:
        filename: The basename a stored state arrived with.
        slot: The slot to stamp into the name.

    Returns:
        The same state named for `slot`, or None if `filename` is not a state name.
    """
    match = _STATE_NAME_RE.match(filename)
    if match is None:
        return None
    return f"{match.group('serial')}.{slot:02d}.p2s"


def _wait_for_sstate_write(
    before: dict[Path, tuple[int, float]], deadline: float, slot: Optional[int] = None
) -> bool:
    """Poll `SSTATE_DIR` until a slot-matching write completes or the deadline passes.

    A write counts as complete once the file's size has been stable for 0.5 s.
    PCSX2 acks PINE saves before writing, so this is the only reliable
    confirmation. A target that disappears mid-write is dropped and the scan
    starts over.

    Args:
        before: Snapshot from `_sstate_snapshot` taken before the save was requested.
        deadline: `time.monotonic` value to give up at.
        slot: Only consider files in this slot; None watches every state.

    Returns:
        True once a new or modified state has settled, False on timeout.
    """
    STABLE_SECS = 0.5
    POLL_SECS = 0.1
    target: Optional[Path] = None
    last_size: Optional[int] = None
    stable_since: Optional[float] = None
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


def newest_state_for_slot(slot: int) -> Optional[Path]:
    """Find the most recently written state in `slot`.

    Args:
        slot: The slot number, matched in both its padded and unpadded spelling.

    Returns:
        The newest matching `.p2s` by mtime, or None when the slot holds nothing.
    """
    if not SSTATE_DIR.is_dir():
        return None
    candidates: list[tuple[float, Path]] = []
    for pattern in {f"*.{slot:02d}.p2s", f"*.{slot}.p2s"}:
        for p in SSTATE_DIR.glob(pattern):
            try:
                candidates.append((p.stat().st_mtime, p))
            except OSError:
                pass
    if not candidates:
        return None
    return max(candidates)[1]


def _pine_recv_exact(sock: _socket.socket, n: int) -> Optional[bytes]:
    """Read exactly `n` bytes from the PINE socket.

    Args:
        sock: A connected PINE socket.
        n: Number of bytes to read.

    Returns:
        The bytes read, or None if the peer closed the connection first.
    """
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def _pine_request(opcode: int, payload: bytes = b"", timeout: float = 5.0) -> Optional[bytes]:
    """Send one PINE request and return the reply body.

    Wire format (little endian): u32 total size, u8 opcode, payload; the reply
    is u32 size, u8 result (0 = OK), payload. Each request opens its own
    connection to `PINE_SOCKET`.

    Args:
        opcode: The PINE message opcode.
        payload: Bytes following the opcode.
        timeout: Socket timeout in seconds for connect, send and receive.

    Returns:
        The reply payload (possibly empty), or None when the socket is down, the peer hangs up, or
        PCSX2 rejects the request with a non-zero result.
    """
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


def _pine_emu_status() -> Optional[int]:
    """Query the VM status over PINE.

    Returns:
        0 running, 1 paused, 2 shutdown, or None if PINE is down or the reply is malformed.
    """
    body = _pine_request(_PINE_MSG_EMU_STATUS, timeout=2.0)
    if body is None or len(body) < 4:
        return None
    return struct.unpack("<I", body[:4])[0]


class Pcsx2(Emulator):
    """PlayStation 2 sessions on pcsx2-qt.

    The broker launches `pcsx2-qt -batch -fullscreen -- <disc>` after forcing
    its settings into PCSX2.ini (PINE on, fullscreen, no shutdown confirm, no
    setup wizard, no state on shutdown, the broker's folder card in Slot 1)
    and binding Pad1 to the Selkies SDL gamepad. Save states are driven over
    the PINE Unix socket: a save command into `STATE_SLOT` followed by a poll
    of the state directory, because PINE acks before the file is written, and
    a load command once a state is confirmed on disk. Every launch starts a
    boot watchdog that polls the VM status; a resume is delivered as a
    deferred load once the VM reports running, and a VM that never runs while
    the process stays alive is flagged as a boot failure, since PCSX2 parks on
    an error dialog instead of exiting.

    Save data lives in a folder memory card the broker owns in Slot 1, so the
    whole-card routes can ship and replace it as an image; a missing card path
    would make PCSX2 create a file card instead, which cannot. States and
    memory cards both ride the save archive. A state file is named for the
    serial PCSX2 reads off the running disc, so pushed names are restamped
    into the broker's slot and the working slot is cleared before a boot, to
    stop the previous session's state being served as this one's.

    Attributes:
        name: RomM platform key, `pcsx2`.
        display_name: Human-readable name shown in the UI.
        save_root: The PCSX2 config root the save subtrees hang off.
        save_subtrees: `memcards` and `sstates`, the directories the save archive carries.
        memory_card_subtree: Subtree the whole-card routes operate on.
        memory_card_marker: File whose presence makes PCSX2 treat a directory as a folder card.
        rom_extensions: Bootable disc formats, best first.
        supports_states: True, states are saved and loaded over PINE.
        state_slot: The one slot the broker works in, echoed back as the effective slot.
        state_dir: Where PCSX2 writes `.p2s` files.
        log_path: The pcsx2-qt log the broker exposes.
        boot_failed: Set by the boot watchdog when the VM never reached a running state.
    """

    name = "pcsx2"
    display_name = "PCSX2"
    save_root = Path("/config/.config/PCSX2")
    save_subtrees = ("memcards", "sstates")
    memory_card_subtree = "memcards"
    memory_card_marker = SLOT1_MARKER
    rom_extensions = ROM_EXTENSIONS
    supports_states = True
    state_slot = STATE_SLOT
    state_dir = SSTATE_DIR
    log_path = PCSX2_LOG_PATH

    def __init__(self) -> None:
        """Set up the process state and the launch sequence counter that fences the watchdog."""
        super().__init__()
        self._launch_seq = 0

    def resolve_rom_file(self, path: Path) -> Optional[Path]:
        """Resolve a RomM path to the disc image to boot.

        A file is taken as is. A directory is searched one level deep for the
        best candidate by `_pick_rom_file`.

        Args:
            path: The ROM file or folder RomM handed over.

        Returns:
            The image to pass to pcsx2-qt, or None when there is nothing bootable.
        """
        if path.is_file():
            # Defense in depth: api.py already validates path is under
            # ROM_ROOT before calling in, but this checks it independently
            # rather than trusting every future caller to do the same.
            try:
                if not path.resolve().is_relative_to(ROM_ROOT):
                    return None
            except OSError:
                return None
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

    def memory_card_path(self, platform: Optional[str] = None) -> Optional[Path]:
        """Return the path of the Slot-1 folder card the whole-card routes ship and replace."""
        return MEMCARD_DIR / SLOT1_CARD_NAME

    def _ensure_folder_card(self) -> None:
        """Have a folder card waiting at the Slot-1 path before PCSX2 opens it.

        A path that is not there is what makes PCSX2 write itself a fresh 8 MB
        file card, and a file card cannot be shipped or replaced as an image,
        so the whole-card routes would refuse the container from then on. A
        failure to create the card is logged, not raised.
        """
        card = MEMCARD_DIR / SLOT1_CARD_NAME
        try:
            memcard.ensure_card(card, SLOT1_MARKER)
        except OSError as exc:
            log.warning("could not create the slot 1 folder card at %s: %s", card, exc)

    def launch(self, rom_path: Path, resume_slot: Optional[int]) -> None:
        """Stop any running instance, prepare the config and card, and start pcsx2-qt.

        The binary comes from env `PCSX2_BIN` (default `pcsx2-qt`). A boot
        watchdog thread is always started; it verifies boot and only delivers
        a state load when `resume_slot` is set.

        Args:
            rom_path: The disc image to boot.
            resume_slot: Slot to load once the VM is running, or None to boot clean.
        """
        self.stop()
        _patch_ini()
        self._ensure_folder_card()
        self.boot_failed = False  # every launch starts clean
        self._launch_seq += 1
        seq = self._launch_seq

        binary = os.environ.get("PCSX2_BIN", "pcsx2-qt")
        log.info("launching pcsx2 (rom=%s, resume_slot=%s)", rom_path, resume_slot)
        self._spawn(
            [binary, "-batch", "-fullscreen", "--", str(rom_path)], base_launch_env()
        )

        # Always: the watchdog verifies boot, and delivers a state only if one
        # was asked for.
        Thread(
            target=self._boot_watchdog, args=(resume_slot, seq), daemon=True
        ).start()

    def _boot_watchdog(self, slot: Optional[int], seq: int) -> None:
        """Verify the launched game reaches a running VM and deliver a deferred state load.

        Polls the PINE status once a second until `RESUME_LOAD_WAIT` runs out.
        Once the VM runs, a requested resume waits `RESUME_LOAD_SETTLE`, then
        for the state file to exist, then loads it. A process that is still
        alive when the deadline passes without the VM ever running is the
        boot-error-dialog case: PCSX2 does not exit, so nothing else in the
        broker would ever notice; `boot_failed` is set for it. The watchdog
        abandons itself whenever `seq` no longer matches the current launch.

        Args:
            slot: Slot to load after boot, or None for boot verification only.
            seq: The launch sequence number this watchdog belongs to.
        """
        deadline = time.monotonic() + RESUME_LOAD_WAIT
        while time.monotonic() < deadline:
            if self._launch_seq != seq:
                log.info("boot watchdog: launch superseded, abandoning")
                return
            if _pine_emu_status() == 0:
                # Booted. Everything from here is the pre-existing resume path.
                if slot is None:
                    return
                time.sleep(RESUME_LOAD_SETTLE)
                if not self.wait_for_state(deadline):
                    log.warning("resume: slot %d never got a state file", slot)
                    return
                if self._launch_seq != seq:
                    return
                ok = self.load_state(slot)
                log.info("resume: deferred load of slot %d %s", slot, "delivered" if ok else "failed")
                return
            time.sleep(1.0)

        # Deadline passed without a running VM.
        if self._launch_seq != seq:
            return
        if self.alive():
            self.boot_failed = True
            log.warning(
                "boot watchdog: VM never reached running state and pcsx2 is "
                "still alive, treating as a boot failure"
            )
        else:
            log.warning("boot watchdog: pcsx2 exited before the VM ever ran")

    def save_state(self, slot: int) -> bool:
        """Save a state into the broker's slot over PINE and wait for it to land.

        `slot` is what RomM asked for and is ignored: this saves into
        `STATE_SLOT` and the caller reads the effective slot back off
        `state_slot`. PINE can address any slot directly, but RomM keeps the
        library of states, so working in one slot is all this needs to do.

        Args:
            slot: The slot RomM requested; not used.

        Returns:
            True once the state file has been written and settled within `PINE_WAIT`, False if
            PINE rejected the command or the write never completed.
        """
        before = _sstate_snapshot()
        if _pine_request(_PINE_MSG_SAVE_STATE, bytes([STATE_SLOT])) is None:
            return False
        return _wait_for_sstate_write(before, time.monotonic() + PINE_WAIT, STATE_SLOT)

    def load_state(self, slot: int) -> bool:
        """Load the broker's slot over PINE.

        PINE acks a load for an empty slot, so an absent file has to be caught
        here or the caller reads a no-op as success.

        Args:
            slot: The slot RomM requested; the broker's `STATE_SLOT` is what gets loaded.

        Returns:
            True when a state file exists and PINE accepted the load, False otherwise.
        """
        if self.state_path() is None:
            log.warning("load state: slot %d holds no state file", STATE_SLOT)
            return False
        return _pine_request(_PINE_MSG_LOAD_STATE, bytes([STATE_SLOT])) is not None

    def state_path(self) -> Optional[Path]:
        """Return the newest state file in the broker's slot, or None when it holds nothing."""
        return newest_state_for_slot(STATE_SLOT)

    def clear_working_slot(self) -> None:
        """Delete every state in the broker's slot before a new session boots.

        A `.p2s` is named for the disc it was taken from, and the serial only
        comes off the running disc, so a leftover cannot be told apart from the
        state of the game about to boot. Anything still here belongs to a
        session that has already exited and whose states RomM holds, so
        dropping it is what stops the last player's save being served as this
        one's. The archive restore and the resume push both land afterwards.
        """
        if not SSTATE_DIR.is_dir():
            return
        for pattern in {f"*.{STATE_SLOT:02d}.p2s", f"*.{STATE_SLOT}.p2s"}:
            for stale in SSTATE_DIR.glob(pattern):
                try:
                    stale.unlink()
                    log.info("cleared stale state %s", stale.name)
                except OSError as exc:
                    log.warning("could not clear stale state %s: %s", stale.name, exc)

    def state_target(self, filename: str) -> Optional[Path]:
        """Map a pushed state's filename to where it may be written.

        PCSX2 finds a state by the serial it reads off the running disc, so
        the serial is what a pushed name has to get right; the slot it was
        captured in is rewritten to this broker's. With the slot already
        holding a state, that name is the one to match, otherwise the serial
        is taken on trust, bounded to a `<serial>.<slot>.p2s` basename in the
        state dir.

        Args:
            filename: The basename RomM is pushing.

        Returns:
            The path to write to, or None when the name is not a state name, carries a path
            component, or does not match the state already in the slot.
        """
        if "/" in filename or filename in ("", ".", ".."):
            return None
        restamped = _restamp_slot(filename, STATE_SLOT)
        if restamped is None:
            return None
        existing = self.state_path()
        if existing is not None:
            return existing if restamped == existing.name else None
        return SSTATE_DIR / restamped

    def save_and_exit(self, slot: Optional[int]) -> dict[str, Any]:
        """Save a state if asked, then stop the emulator.

        Args:
            slot: Slot RomM asked to save into (resolved to `STATE_SLOT`), or None to exit
                without saving a state.

        Returns:
            A dict with `state_saved` (bool), `state_slot` (the effective slot, or None when no
            save was requested) and `state_file` (a dict of `path`, `size` and `mtime` for the
            saved state, or None).
        """
        saved = False
        state_file: Optional[dict[str, Any]] = None
        if slot is not None and self.alive():
            saved = self.save_state(slot)
            if saved:
                p = self.state_path()
                if p is not None:
                    try:
                        st = p.stat()
                    except OSError as exc:
                        log.warning("could not stat saved state %s: %s", p, exc)
                        saved = False
                    else:
                        state_file = {"path": str(p), "size": st.st_size, "mtime": st.st_mtime}
        self.stop()
        return {
            "state_saved": saved,
            "state_slot": STATE_SLOT if slot is not None else None,
            "state_file": state_file,
        }

    def stop(self) -> None:
        """Invalidate any in-flight boot watchdog, and any state load it might still deliver, then kill."""
        self._launch_seq += 1
        super().stop()
