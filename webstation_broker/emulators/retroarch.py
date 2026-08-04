"""RetroArch launcher for any libretro core: platform->core mapping,
buildbot core download, and the stdin command protocol for save/state/quit.

This is the general purpose provider: instead of one class per emulator we
keep a map from RomM platform slug to libretro core.

RetroArch is the user's own desktop app, so we never touch its config file.
The launch uses `--appendconfig <broker.cfg>` to layer in only the
protocol-required settings on top of the user's config: the stdin command
interface, and the broker-managed save directories so the save archive logic
tracks real files.

Control plane: RetroArch's stdin command interface (config key
`stdin_cmd_enable`) reads newline-delimited commands from stdin and writes
replies to *stdout*. Commands:

  SAVE_STATE           -> no reply; dispatches CMD_EVENT_SAVE_STATE from the
                          runloop (the save-state hotkey path)
  LOAD_STATE_SLOT <n>  -> bare echo "LOAD_STATE_SLOT <n>" (no success bit)
  SAVE_FILES           -> "OK" / "NO" (newline-terminated)
  GET_STATUS           -> "GET_STATUS PLAYING <core_id>,<basename>\n"
                          or "GET_STATUS CONTENTLESS"
  QUIT                 -> no reply; queued for the runloop

Saves are confirmed on the filesystem instead of from a reply.

State file naming:
  <content_basename>.state    slot 0
  <content_basename>.state<n> slot n
  SRAM: <content_basename>.srm under the savefile dir.

Because stdout carries only command replies here, the child is spawned with
a real stdout pipe drained by a reader thread, unlike the shared _spawn
which merges stderr into stdout (that would corrupt the reply stream).
"""

import io
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import zipfile
from pathlib import Path

import httpx

from .base import Emulator, base_launch_env

log = logging.getLogger(__name__)

ROM_ROOT = Path(os.environ.get("ROM_ROOT", "/romm"))

XDG_DATA_HOME = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share")
# RetroArch's default Linux cores dir; dropping cores here is what makes the
# desktop RetroArch "just work" without its in-app core downloader.
CORES_DIR = Path(
    os.environ.get(
        "RETROARCH_CORES_DIR", str(Path(XDG_DATA_HOME) / "RetroArch" / "cores")
    )
)
# Buildbot ships every core as <core>_libretro.so.zip;
CORES_BASE_URL = os.environ.get(
    "RETROARCH_CORES_BASE_URL",
    "https://buildbot.libretro.com/nightly/linux/x86_64/latest",
)

# Broker-managed save data and the append-config we layer onto the user's
# RetroArch config at launch.
RA_DATA_DIR = Path(os.environ.get("RETROARCH_DATA_DIR", "/config/.retroarch"))
STATE_DIR = RA_DATA_DIR / "states"
SAVE_DIR = RA_DATA_DIR / "saves"
BROKER_CFG = RA_DATA_DIR / "broker.cfg"
RA_LOG_PATH = Path(os.environ.get("RETROARCH_LOG_PATH", "/config/retroarch.log"))

# Protocol timings.
SAVE_FILES_WAIT = float(os.environ.get("RETROARCH_SAVE_FILES_WAIT", "10.0"))
STATE_CONFIRM_WAIT = float(os.environ.get("RETROARCH_STATE_CONFIRM_WAIT", "10.0"))
QUIT_WAIT = float(os.environ.get("RETROARCH_QUIT_WAIT", "10.0"))
QUIT_CONFIRM_GAP = float(os.environ.get("RETROARCH_QUIT_CONFIRM_GAP", "0.1"))
RESUME_LOAD_WAIT = float(os.environ.get("RETROARCH_RESUME_WAIT", "90.0"))
RESUME_LOAD_SETTLE = float(os.environ.get("RETROARCH_RESUME_SETTLE", "3.0"))
CORE_DOWNLOAD_TIMEOUT = float(os.environ.get("RETROARCH_CORE_DOWNLOAD_TIMEOUT", "180"))

# RomM platform slug -> libretro core. Extensions order doubles as the
# preference order when a folder holds several candidates.
#
# `savestate` is assumed true; only specialized cores opt out.
PLATFORMS: dict[str, dict] = {
    "ngc": {
        "core": "dolphin",
        "extensions": (".rvz", ".gcz", ".iso", ".gcm", ".wbfs", ".chd", ".wad", ".dol", ".elf"),
        "save_subtrees": (
            "states",
            "saves/dolphin-emu/User/GC",
            "saves/dolphin-emu/User/Wii/title",
            "saves/dolphin-emu/User/Wii/shared2",
        ),
    },
    "wii": {
        "core": "dolphin",
        "extensions": (".rvz", ".gcz", ".iso", ".gcm", ".wbfs", ".ciso", ".chd", ".wad", ".dol", ".elf"),
        "save_subtrees": (
            "states",
            "saves/dolphin-emu/User/GC",
            "saves/dolphin-emu/User/Wii/title",
            "saves/dolphin-emu/User/Wii/shared2",
        ),
    },
    "snes": {
        "core": "snes9x",
        "extensions": (".sfc", ".smc", ".fig", ".swc", ".st", ".gd3", ".gd7", ".dx2", ".bs", ".bin"),
    },
    "n64": {
        "core": "mupen64plus_next",
        "extensions": (".n64", ".z64", ".v64", ".rom", ".ndd"),
    },
    "dc": {
        "core": "flycast",
        "extensions": (".gdi", ".cdi", ".chd", ".cue", ".m3u", ".iso", ".bin"),
    },
    "saturn": {
        "core": "yabasanshiro",
        "extensions": (".cue", ".chd", ".iso", ".bin", ".m3u", ".ccd", ".toc"),
    },
    "psp": {
        "core": "ppsspp",
        "extensions": (".iso", ".cso", ".pbp", ".chd", ".elf", ".prx"),
    },
    "nds": {
        "core": "melonds",
        "extensions": (".nds", ".srl"),
    },
    "3ds": {
        "core": "citra",
        "extensions": (".3ds", ".cci", ".cxi", ".cia", ".3dsx", ".app", ".srl"),
    },
    "arcade": {
        "core": "fbneo",
        "extensions": (".zip", ".7z", ".chd"),
    },
    "genesis": {
        "core": "genesis_plus_gx",
        "extensions": (".md", ".gen", ".smd", ".bin", ".sg", ".sms", ".gg", ".cue"),
    },
}

_ROM_SEARCH_GLOBS = ("*", "*/*")
_ADDON_RE = re.compile(
    r"(?:^|[^a-z0-9])(?:update|upd|dlc|patch)(?:[^a-z0-9]|$)", re.IGNORECASE
)
# Disc numbering keeps a multi-disc game booting the same disc each session,
# so a save state taken for Disc 1 resumes on Disc 1.
_DISC_RE = re.compile(r"(?:^|[^a-z0-9])(?:disc|disk|cd)[\s._-]*(\d+)", re.IGNORECASE)


def _platform_info(platform: str | None) -> dict | None:
    if not platform:
        return None
    return PLATFORMS.get(platform.lower())


def _ensure_core(core: str) -> Path:
    """Return the core's .so, downloading it from the buildbot if missing."""
    so = CORES_DIR / f"{core}_libretro.so"
    if so.is_file():
        return so
    CORES_DIR.mkdir(parents=True, exist_ok=True)
    url = f"{CORES_BASE_URL}/{core}_libretro.so.zip"
    log.info("retroarch: downloading core %s from %s", core, url)
    try:
        resp = httpx.get(url, follow_redirects=True, timeout=CORE_DOWNLOAD_TIMEOUT)
        resp.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            names = [n for n in zf.namelist() if n.endswith("_libretro.so")]
            if not names:
                raise RuntimeError(f"core zip for {core} contained no _libretro.so")
            data = zf.read(sorted(names)[0])
    except (httpx.HTTPError, zipfile.BadZipFile, OSError) as exc:
        raise RuntimeError(f"failed to download libretro core {core}: {exc}") from exc
    tmp = CORES_DIR / f"{core}_libretro.so.tmp"
    tmp.write_bytes(data)
    tmp.chmod(0o755)
    os.replace(tmp, so)
    log.info("retroarch: core %s installed at %s", core, so)
    return so


def _write_broker_cfg() -> Path:
    """Minimal per-launch config, applied *on top of* the user's config.
    Only the stdin interface and the broker save dirs; nothing else."""
    RA_DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    SAVE_DIR.mkdir(parents=True, exist_ok=True)
    cfg = (
        'stdin_cmd_enable = "true"\n'
        f'savestate_directory = "{STATE_DIR}"\n'
        f'savefile_directory = "{SAVE_DIR}"\n'
        # SAVE_STATE saves to the current state_slot; pin it to the broker's
        # slot so saves and resumes agree.
        'state_slot = "10"\n'
        'savestate_auto_save = "false"\n'
        'savestate_auto_load = "false"\n'
        'savestate_auto_index = "false"\n'
        # The stdin QUIT must exit immediately, not arm a "press again"
        # confirmation.
        'confirm_quit = "false"\n'
        'quit_press_twice = "false"\n'
    )
    tmp = BROKER_CFG.with_suffix(".tmp")
    tmp.write_text(cfg)
    os.replace(tmp, BROKER_CFG)
    return BROKER_CFG


def _state_snapshot(dir_path: Path, base: str) -> dict:
    """{path: (size, mtime)} for state files of one content basename.
    Recursive because cores like dolphin redirect state paths into their own
    subdir (e.g. states/dolphin-emu/), where a flat lookup would never see
    the write."""
    if not dir_path.is_dir():
        return {}
    prefix = f"{base}.state"
    snap: dict = {}
    try:
        for p in dir_path.rglob("*"):
            if not p.is_file() or not p.name.startswith(prefix):
                continue
            st = p.stat()
            snap[p] = (st.st_size, st.st_mtime)
    except OSError:
        pass
    return snap


def _wait_for_state_file(
    before: dict, dir_path: Path, base: str, slot: int, timeout: float
) -> bool:
    """Poll until `slot`'s state file is rewritten and its size is stable,
    which is the only reliable confirmation a save-state landed."""
    STABLE = 0.5
    POLL = 0.1
    target_names = {
        base + (".state" if slot <= 0 else f".state{slot}"),
        base + ".state",
    }
    deadline = time.monotonic() + timeout
    last_size: int | None = None
    stable_since: float | None = None
    seen_change = False
    while time.monotonic() < deadline:
        after = _state_snapshot(dir_path, base)
        targets = [p for p in after if p.name in target_names]
        if not targets:
            time.sleep(POLL)
            continue
        cur_path = max(targets, key=lambda p: after[p][1])
        cur = after[cur_path]
        prev = before.get(cur_path)
        if not seen_change and (prev is None or prev[1] != cur[1]):
            seen_change = True
        if not seen_change:
            time.sleep(POLL)
            continue
        if last_size is None or cur[0] != last_size:
            last_size = cur[0]
            stable_since = time.monotonic()
        elif time.monotonic() - stable_since >= STABLE:
            log.info("retroarch: save state write complete: %s (%d bytes)", cur_path.name, last_size)
            return True
        time.sleep(POLL)
    log.warning("retroarch: state file not confirmed on disk within %.1fs", timeout)
    return False


def _newest_state(dir_path: Path, base: str, slot: int) -> Path | None:
    names = {base + (".state" if slot <= 0 else f".state{slot}"), base + ".state"}
    best: tuple[float, Path] | None = None
    try:
        for p in dir_path.rglob(f"{base}.state*"):
            if not p.is_file() or p.name not in names:
                continue
            st = p.stat()
            if best is None or st.st_mtime > best[0]:
                best = (st.st_mtime, p)
    except OSError:
        return None
    return best[1] if best is not None else None


def _disc_number(rel: Path) -> int:
    match = _DISC_RE.search(str(rel))
    if match is None:
        return 1
    return max(1, int(match.group(1)))


def _pick_rom_file(candidates, base: Path, extensions: tuple[str, ...]) -> Path | None:
    ranked = []
    for p in candidates:
        if p.name.startswith("."):
            continue
        ext = p.suffix.lower()
        if ext not in extensions:
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
        is_addon = 1 if _ADDON_RE.search(str(rel)) else 0
        ranked.append(
            (
                is_addon,
                _disc_number(rel),
                extensions.index(ext),
                len(rel.parts),
                p.name.lower(),
                real,
            )
        )
    if not ranked:
        return None
    return min(ranked)[5]


class Retroarch(Emulator):
    name = "retroarch"
    display_name = "RetroArch"
    save_root = RA_DATA_DIR
    log_path = RA_LOG_PATH
    # QUIT walks a graceful core teardown; give it room before SIGTERM.
    term_timeout = float(os.environ.get("RETROARCH_STOP_WAIT", "15"))

    def __init__(self):
        super().__init__()
        # Set from the activate payload's rom.platform before launch.
        self.platform: str | None = None
        self._rom_base: str = ""
        self._launch_seq = 0
        self._stdout_buf = bytearray()
        self._stdout_lock = threading.Lock()
        self._reader: threading.Thread | None = None

    @property
    def save_subtrees(self) -> tuple[str, ...]:
        """Per-platform dump scope; cores whose savefile dir is also their
        app-data dir (dolphin) narrow the archive to real save files."""
        info = _platform_info(self.platform)
        scoped = info.get("save_subtrees") if info else None
        return scoped or ("states", "saves")

    @property
    def rom_extensions(self):
        info = _platform_info(self.platform)
        return info["extensions"] if info else ()

    def resolve_rom_file(self, path: Path) -> Path | None:
        info = _platform_info(self.platform)
        if info is None:
            log.warning(
                "retroarch: no core mapped for platform %r; mapped: %s",
                self.platform,
                ", ".join(sorted(PLATFORMS)),
            )
            return None
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
        return _pick_rom_file(candidates, path, info["extensions"])

    def _spawn_ra(self, cmd: list[str], env: dict[str, str]) -> None:
        """Spawn with a real stdout pipe (stderr to the log). stdout carries
        the command replies, so it must stay clean."""
        log_fh = None
        try:
            log_fh = open(self.log_path, "ab", buffering=0)
            log_fh.write(
                f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} launch ({' '.join(cmd)}) ===\n".encode()
            )
        except OSError:
            log_fh = None
        try:
            self._proc = subprocess.Popen(
                cmd,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=log_fh if log_fh else subprocess.DEVNULL,
                start_new_session=True,
            )
        finally:
            if log_fh:
                log_fh.close()
        self._stdout_buf = bytearray()
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader.start()

    def _read_stdout(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        fd = proc.stdout.fileno()
        try:
            while True:
                chunk = os.read(fd, 4096)
                if not chunk:
                    break
                with self._stdout_lock:
                    self._stdout_buf.extend(chunk)
        except (OSError, ValueError):
            pass

    def _wait_for_reply(self, prefixes, timeout: float) -> str | None:
        """Wait for a stdout reply matching one of `prefixes`; consume it and
        return it. Replies are newline-terminated except the bare echoes of
        the *_SLOT commands and GET_STATUS CONTENTLESS."""
        if isinstance(prefixes, str):
            prefixes = (prefixes,)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.alive():
                return None
            with self._stdout_lock:
                buf = bytes(self._stdout_buf)
            text = buf.decode("utf-8", errors="replace")
            best: tuple[int, str] | None = None
            for pfx in prefixes:
                idx = text.find(pfx)
                if idx != -1 and (best is None or idx < best[0]):
                    best = (idx, pfx)
            if best is not None:
                idx = best[0]
                end = text.find("\n", idx)
                if end == -1:
                    line = text[idx:]
                    with self._stdout_lock:
                        del self._stdout_buf[:len(text)]
                else:
                    line = text[idx:end]
                    with self._stdout_lock:
                        del self._stdout_buf[:end + 1]
                return line
            time.sleep(0.05)
        return None

    def _send(
        self, cmd: str, wait_prefix=None, timeout: float = 5.0
    ) -> str | None:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.poll() is not None:
            return None
        try:
            proc.stdin.write(f"{cmd}\n".encode())
            proc.stdin.flush()
        except (BrokenPipeError, OSError):
            return None
        if wait_prefix:
            return self._wait_for_reply(wait_prefix, timeout)
        return None

    def launch(self, rom_path: Path, resume_slot: int | None) -> None:
        self.stop()
        info = _platform_info(self.platform)
        if info is None:
            raise RuntimeError(
                f"no retroarch core mapped for platform {self.platform!r}; "
                f"mapped: {', '.join(sorted(PLATFORMS))}"
            )
        core = _ensure_core(info["core"])
        cfg_path = _write_broker_cfg()

        binary = os.environ.get("RETROARCH_BIN", "retroarch")
        if "/" not in binary and shutil.which(binary) is None:
            raise RuntimeError(f"retroarch binary not found in PATH: {binary}")

        self._rom_base = rom_path.stem
        self._launch_seq += 1
        seq = self._launch_seq

        cmd = [
            binary,
            "-L",
            str(core),
            "--appendconfig",
            str(cfg_path),
            "--fullscreen",
            str(rom_path),
        ]
        log.info(
            "launching retroarch (core=%s, rom=%s, resume_slot=%s)",
            core.name,
            rom_path,
            resume_slot,
        )
        self._spawn_ra(cmd, base_launch_env())

        if resume_slot:
            threading.Thread(
                target=self._deferred_load_state, args=(resume_slot, seq), daemon=True
            ).start()

    def _deferred_load_state(self, slot: int, seq: int) -> None:
        """Load `slot` once RetroArch reports the content PLAYING; cores with
        no game running yet (Dolphin boot screen) reject loads."""
        deadline = time.monotonic() + RESUME_LOAD_WAIT
        while time.monotonic() < deadline:
            if self._launch_seq != seq or not self.alive():
                return
            reply = self._send("GET_STATUS", wait_prefix="GET_STATUS", timeout=2.0)
            if reply and reply.startswith("GET_STATUS PLAYING"):
                time.sleep(RESUME_LOAD_SETTLE)
                if self._launch_seq != seq:
                    return
                ok = self._send(
                    f"LOAD_STATE_SLOT {slot}",
                    wait_prefix="LOAD_STATE_SLOT",
                    timeout=RESUME_LOAD_WAIT,
                )
                log.info(
                    "resume: requested load of slot %d (%s)",
                    slot,
                    "delivered" if ok is not None else "no reply",
                )
                return
            time.sleep(1.0)
        log.warning(
            "resume: retroarch never reported PLAYING, slot %d not loaded", slot
        )

    def save_state(self, slot: int) -> bool:
        before = _state_snapshot(STATE_DIR, self._rom_base)
        self._send("SAVE_STATE")
        return _wait_for_state_file(
            before, STATE_DIR, self._rom_base, slot, STATE_CONFIRM_WAIT
        )

    def save_and_exit(self, slot: int) -> dict:
        saved = False
        state_file = None
        if self.alive():
            info = _platform_info(self.platform)
            if info is None or info.get("savestate", True):
                saved = self.save_state(slot)
                if saved:
                    p = _newest_state(STATE_DIR, self._rom_base, slot)
                    if p is not None:
                        st = p.stat()
                        state_file = {"path": str(p), "size": st.st_size, "mtime": st.st_mtime}
            # Flush SRAM so the save dump ships current save data.
            self._send("SAVE_FILES", wait_prefix=("OK", "NO"), timeout=SAVE_FILES_WAIT)
        self._quit()
        return {"state_saved": saved, "state_slot": slot, "state_file": state_file}

    def _quit(self) -> None:
        proc = self._proc
        if proc is not None and proc.poll() is None and proc.stdin is not None:
            log.info("stopping %s (pid %d) via QUIT", self.name, proc.pid)
            try:
                proc.stdin.write(b"QUIT\n")
                proc.stdin.flush()
            except (BrokenPipeError, OSError):
                pass
            try:
                proc.wait(timeout=QUIT_CONFIRM_GAP)
                self._proc = None
                log.info("%s exited gracefully", self.name)
                return
            except subprocess.TimeoutExpired:
                pass
            if proc.poll() is None:
                log.info(
                    "%s (pid %d) still up after first QUIT, pressing again",
                    self.name, proc.pid,
                )
                try:
                    proc.stdin.write(b"QUIT\n")
                    proc.stdin.flush()
                except (BrokenPipeError, OSError):
                    pass
            try:
                proc.wait(timeout=QUIT_WAIT)
                self._proc = None
                log.info("%s exited gracefully", self.name)
                return
            except (BrokenPipeError, OSError):
                pass
            except subprocess.TimeoutExpired:
                log.warning(
                    "%s did not exit after QUIT, escalating to SIGTERM", self.name
                )
        super().stop()

    def stop(self) -> None:
        # Invalidate any in-flight deferred state load before the kill.
        self._launch_seq += 1
        super().stop()
