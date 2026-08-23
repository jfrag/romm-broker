"""Dolphin (GameCube/Wii) launcher: ROM resolution, save states over the
emulator's own hotkeys, and resume by boot-time state load.

Dolphin has no control socket, but it takes every setting it needs on the
command line: `-C` writes into the layered config, so nothing here has to
patch an INI, and `-s` loads a state file by path at boot, so a resume that
is already on disk never depends on a keystroke landing. What is left is the
mid-session save, which only the hotkey can reach; the broker runs as the
same user as the session, so xdotool talks to Xwayland directly.

Save states are not thumbnailed here. The frame comes off the streamed canvas
in the browser, which is the only capture that cannot stall the emulator.
"""

import logging
import os
import re
import subprocess
import time
from pathlib import Path
from threading import Thread

from .base import Emulator, base_launch_env

log = logging.getLogger(__name__)

ROM_ROOT = Path(os.environ.get("ROM_ROOT", "/romm"))

USER_DIR = Path(os.environ.get("DOLPHIN_USER_DIR", "/config/.local/share/dolphin-emu"))
STATE_DIR = USER_DIR / "StateSaves"
CONFIG_DIR = USER_DIR / "Config"
DOLPHIN_LOG_PATH = Path(os.environ.get("DOLPHIN_LOG_PATH", "/config/dolphin.log"))

# The one slot the broker works in. RomM holds the library of states, so a
# requested slot resolves to this one and the routes echo the effective slot.
STATE_SLOT = int(os.environ.get("DOLPHIN_STATE_SLOT", "1"))

# EXIDeviceType::MemoryCardFolder, the GC slot A device that keeps saves as
# loose .gci files under GC/<region>/Card A rather than one .raw card image.
GC_SLOT_A_DEVICE = 8

# Dolphin's own defaults: Shift+F<n> saves slot n, F<n> loads it.
SAVE_KEY = f"shift+F{STATE_SLOT}"
LOAD_KEY = f"F{STATE_SLOT}"

STATE_WAIT = float(os.environ.get("DOLPHIN_STATE_WAIT", "20.0"))
RESUME_LOAD_WAIT = float(os.environ.get("DOLPHIN_RESUME_LOAD_WAIT", "90.0"))
# How long the window has to be up before a hotkey is worth sending. Dolphin
# maps its window before the core is running, and a load that early is dropped.
RESUME_LOAD_SETTLE = float(os.environ.get("DOLPHIN_RESUME_LOAD_SETTLE", "5.0"))

# OGL over Vulkan by default: RADV on the integrated AMD parts these containers
# run on has been the less reliable of the two.
VIDEO_BACKEND = os.environ.get("DOLPHIN_VIDEO_BACKEND", "OGL")

# Discs Dolphin boots, best first, so a folder holding several candidates picks
# the compressed image over the raw one beside it.
ROM_EXTENSIONS = (".rvz", ".wia", ".gcz", ".iso", ".gcm", ".ciso", ".wbfs", ".wad", ".dol", ".elf")
_ROM_SEARCH_GLOBS = ("*", "*/*")
_DISC_RE = re.compile(r"(?:^|[^a-z0-9])(?:disc|disk|cd)[\s._-]*(\d+)", re.IGNORECASE)

# "<game id>.s01", the name Dolphin builds for a save state.
_STATE_NAME_RE = re.compile(r"^(?P<game>[^/]+)\.s\d{2}$")

_XDOTOOL = os.environ.get("XDOTOOL_BIN", "xdotool")
# Dolphin's render window, its main window and its dialogs all share this WM
# class. Only the render window titles itself with the running game, in a
# "Dolphin <ver> | JIT64 | OpenGL | <game>" line, and only it takes hotkeys.
_WINDOW_CLASS = "dolphin-emu"
_RENDER_TITLE_MARK = " | "


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


# Selkies presents the browser gamepad as an SDL device, and Dolphin ships no
# default binding for one, so an unconfigured container has no usable pad.
_GCPAD_TEMPLATE = """[GCPad{n}]
Device = SDL/{i}/Microsoft X-Box 360 pad
Buttons/A = `Button E`
Buttons/B = `Button S`
Buttons/X = `Button N`
Buttons/Y = `Button W`
Buttons/Z = `Shoulder L`
Buttons/Start = Start
Main Stick/Up = `Left Y+`
Main Stick/Down = `Left Y-`
Main Stick/Left = `Left X-`
Main Stick/Right = `Left X+`
Main Stick/Calibration = 100.00 141.42 100.00 141.42 100.00 141.42 100.00 141.42
C-Stick/Up = `Right Y+`
C-Stick/Down = `Right Y-`
C-Stick/Left = `Right X-`
C-Stick/Right = `Right X+`
C-Stick/Calibration = 100.00 141.42 100.00 141.42 100.00 141.42 100.00 141.42
Triggers/L = `Trigger L`
Triggers/R = `Trigger R`
D-Pad/Up = `Pad N`
D-Pad/Down = `Pad S`
D-Pad/Left = `Pad W`
D-Pad/Right = `Pad E`
Triggers/L-Analog = `Thumb L`
Triggers/R-Analog = `Thumb R`
"""


def _seed_gcpad() -> None:
    """Write the pad bindings once, if the file is not already there.

    Seeded rather than patched so a player's own remapping, which Dolphin
    writes back to this same file, survives every later launch.
    """
    path = CONFIG_DIR / "GCPadNew.ini"
    if path.exists():
        return
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "".join(_GCPAD_TEMPLATE.format(n=i + 1, i=i) for i in range(4))
        )
        log.info("seeded %s", path)
    except OSError as exc:
        log.warning("could not seed the pad bindings at %s: %s", path, exc)


def _state_for_slot(slot: int) -> Path | None:
    """The newest state in `slot`, or None if it holds nothing."""
    if not STATE_DIR.is_dir():
        return None
    candidates = []
    for p in STATE_DIR.glob(f"*.s{slot:02d}"):
        try:
            candidates.append((p.stat().st_mtime, p))
        except OSError:
            pass
    if not candidates:
        return None
    return max(candidates)[1]


def _snapshot() -> dict:
    if not STATE_DIR.is_dir():
        return {}
    snap = {}
    for p in STATE_DIR.glob(f"*.s{STATE_SLOT:02d}"):
        try:
            st = p.stat()
            snap[p] = (st.st_size, st.st_mtime)
        except OSError:
            pass
    return snap


def _wait_for_state_write(before: dict, deadline: float) -> bool:
    """Poll the slot until a write completes (size stable for 0.5 s) or the
    deadline passes. The hotkey is fire-and-forget, so the file appearing and
    settling is the only confirmation there is."""
    STABLE_SECS = 0.5
    POLL_SECS = 0.1
    target: Path | None = None
    last_size: int | None = None
    stable_since: float | None = None
    while time.monotonic() < deadline:
        after = _snapshot()
        if target is None:
            for p, (size, mtime) in after.items():
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


def _restamp_slot(filename: str, slot: int) -> str | None:
    """The same state named for `slot`, or None if it is not a state name.

    Dolphin resolves a state by the game id in the name, so that is what has to
    survive the trip; the slot a stored capture happens to carry is rewritten
    into this broker's one working slot."""
    match = _STATE_NAME_RE.match(filename)
    if match is None:
        return None
    return f"{match.group('game')}.s{slot:02d}"


class Dolphin(Emulator):
    name = "dolphin"
    display_name = "Dolphin"
    save_root = USER_DIR
    # GC holds the memory cards, Wii the NAND. GC's card also rides the
    # whole-card routes when a container opts in (memory_card_subtree below);
    # Wii has no physical card, so its NAND only ever moves through the save
    # archive.
    save_subtrees = ("StateSaves", "GC", "Wii")
    memory_card_subtree = "GC"
    rom_extensions = ROM_EXTENSIONS
    supports_states = True
    state_slot = STATE_SLOT
    state_dir = STATE_DIR
    log_path = DOLPHIN_LOG_PATH

    def __init__(self):
        super().__init__()
        self._launch_seq = 0

    def memory_card_path(self, platform: str | None = None) -> Path | None:
        """The whole GC/ tree, or None for Wii (NAND, no physical card).

        Returns the tree rather than one region's Card A folder: Dolphin
        buckets a GCI folder card by the disc's region (GC/<region>/Card A),
        and a library can mix regions, so syncing has to carry all of them
        rather than guessing one."""
        return USER_DIR / "GC" if platform == "ngc" else None

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

    def _xdotool(self, *args: str) -> str | None:
        """Run xdotool, returning its stdout, or None if it failed."""
        try:
            result = subprocess.run(
                [_XDOTOOL, *args],
                env=base_launch_env(),
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.warning("xdotool %s failed: %s", " ".join(args), exc)
            return None
        if result.returncode != 0:
            log.warning("xdotool %s: %s", " ".join(args), result.stderr.strip())
            return None
        return result.stdout

    def _render_window(self) -> str | None:
        """The window id Dolphin renders the game into, or None.

        Picked by title rather than by taking the first match, because the main
        window and any dialog carry the same class, and a hotkey sent at either
        of those does nothing.
        """
        out = self._xdotool("search", "--class", _WINDOW_CLASS)
        if out is None:
            return None
        for win_id in out.split():
            name = self._xdotool("getwindowname", win_id)
            if name and _RENDER_TITLE_MARK in name:
                return win_id
        log.warning("no dolphin render window found")
        return None

    def _send_key(self, key: str) -> bool:
        """Focus the render window and send `key` through XTEST.

        Activating first is what makes this survive the player clicking back
        into the page: XTEST delivers to whatever holds focus, so a key sent at
        an unfocused Dolphin goes to the desktop instead.
        """
        win_id = self._render_window()
        if win_id is None:
            return False
        if self._xdotool("windowactivate", "--sync", win_id) is None:
            return False
        return self._xdotool("key", "--clearmodifiers", key) is not None

    def launch(self, rom_path: Path, resume_slot: int | None) -> None:
        self.stop()
        _seed_gcpad()
        self._launch_seq += 1
        seq = self._launch_seq

        binary = os.environ.get("DOLPHIN_BIN", "dolphin-emu")
        env = base_launch_env()
        # Qt would pick Wayland, where the save hotkey could never be injected.
        env["QT_QPA_PLATFORM"] = "xcb"

        cmd = [
            binary,
            "-b",
            "-u", str(USER_DIR),
            "-v", VIDEO_BACKEND,
            "-C", "Dolphin.Display.Fullscreen=True",
            "-C", "Dolphin.Interface.ConfirmStop=False",
            # A modal panic dialog in a container nobody can click is a hang.
            "-C", "Dolphin.Interface.UsePanicHandlers=False",
            # First run otherwise opens a consent dialog that takes focus off
            # the render window, which is where the save hotkey has to land.
            "-C", "Dolphin.Analytics.PermissionAsked=True",
            "-C", "Dolphin.Analytics.Enabled=False",
            # GCI folder, which is also Dolphin's own default. Pinned because
            # the card sync has to know which layout it is reading, and a
            # default that moved would silently change where saves live.
            "-C", f"Dolphin.Core.SlotA={GC_SLOT_A_DEVICE}",
        ]

        # A state already on disk (restored from the save archive) loads at boot,
        # which is both more reliable than the hotkey and invisible to the player.
        resume_path = _state_for_slot(STATE_SLOT) if resume_slot is not None else None
        if resume_path is not None:
            cmd += ["-s", str(resume_path)]

        cmd += ["-e", str(rom_path)]
        log.info("launching dolphin (rom=%s, resume_slot=%s)", rom_path, resume_slot)
        self._spawn(cmd, env)

        # RomM pushes its resume pick after activate returns, so a slot that was
        # empty at launch can still fill. That one has to go in over the hotkey.
        if resume_slot is not None and resume_path is None:
            Thread(target=self._deferred_load_state, args=(seq,), daemon=True).start()

    def _deferred_load_state(self, seq: int) -> None:
        deadline = time.monotonic() + RESUME_LOAD_WAIT
        if not self.wait_for_state(deadline):
            log.warning("resume: no state file ever arrived")
            return
        if self._launch_seq != seq:
            log.info("resume: launch superseded, load abandoned")
            return
        time.sleep(RESUME_LOAD_SETTLE)
        if self._launch_seq != seq:
            return
        ok = self.load_state(STATE_SLOT)
        log.info("resume: deferred load %s", "delivered" if ok else "failed")

    def save_state(self, slot: int) -> bool:
        """`slot` is what RomM asked for and is ignored: this saves into
        STATE_SLOT and the caller reads the effective slot back off
        state_slot."""
        before = _snapshot()
        if not self._send_key(SAVE_KEY):
            return False
        return _wait_for_state_write(before, time.monotonic() + STATE_WAIT)

    def load_state(self, slot: int) -> bool:
        # The hotkey is silent on an empty slot, so an absent file has to be
        # caught here or the caller reads a no-op as success.
        if self.state_path() is None:
            log.warning("load state: slot %d holds no state file", STATE_SLOT)
            return False
        return self._send_key(LOAD_KEY)

    def state_path(self) -> Path | None:
        return _state_for_slot(STATE_SLOT)

    def clear_working_slot(self) -> None:
        """A state is named for the game it was taken from, and the game id only
        comes off the running disc, so a leftover cannot be told apart from the
        state of the game about to boot. Anything still here belongs to a
        session that has already exited and whose states RomM holds."""
        if not STATE_DIR.is_dir():
            return
        for stale in STATE_DIR.glob(f"*.s{STATE_SLOT:02d}"):
            try:
                stale.unlink()
                log.info("cleared stale state %s", stale.name)
            except OSError as exc:
                log.warning("could not clear stale state %s: %s", stale.name, exc)

    def state_target(self, filename: str) -> Path | None:
        """With the slot already holding a state, a pushed name has to match it;
        otherwise the game id is taken on trust, bounded to a `<game>.s<slot>`
        basename in the state dir."""
        if "/" in filename or filename in ("", ".", ".."):
            return None
        restamped = _restamp_slot(filename, STATE_SLOT)
        if restamped is None:
            return None
        existing = self.state_path()
        if existing is not None:
            return existing if restamped == existing.name else None
        return STATE_DIR / restamped

    def _drop_undo_buffer(self) -> None:
        """Delete the undo-load-state buffer before the save archive is built.

        Dolphin rewrites this on every state load, so it lands in the dump as a
        second full-size copy of a state RomM is already storing, for the sake
        of an undo hotkey a streaming session has no way to press.
        """
        undo = STATE_DIR / "lastState.sav"
        try:
            undo.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("could not drop the undo state buffer: %s", exc)

    def save_and_exit(self, slot: int | None) -> dict:
        saved = False
        state_file = None
        if slot is not None and self.alive():
            saved = self.save_state(slot)
            if saved:
                p = self.state_path()
                if p is not None:
                    try:
                        st = p.stat()
                    except OSError as exc:
                        log.warning("could not stat saved state %s: %s", p, exc)
                    else:
                        state_file = {"path": str(p), "size": st.st_size, "mtime": st.st_mtime}
        self.stop()
        self._drop_undo_buffer()
        return {
            "state_saved": saved,
            "state_slot": STATE_SLOT if slot is not None else None,
            "state_file": state_file,
        }

    def stop(self) -> None:
        # Invalidate any in-flight deferred state load before the kill.
        self._launch_seq += 1
        super().stop()
