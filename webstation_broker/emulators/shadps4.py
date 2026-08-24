"""shadPS4 (PlayStation 4) launcher: binary selection, ROM resolution, and IPC-driven shutdown.

shadPS4 has no save states. Persistence is the game's own save data, which
the game commits to plain host files under
`<data>/home/<user_id>/savedata/<game_serial>/<slot>/`. Save paths are keyed
by the game serial, so shipping the whole `home/1000/savedata` subtree makes
a save archive restored into a fresh container line up with the titles it
belongs to.

Control plane: shadPS4's IPC protocol (`SHADPS4_ENABLE_IPC=true`) reads
commands from stdin. We feed RUN then START so the game boots headlessly,
and STOP for a graceful quit (it pushes SDL_EVENT_QUIT, the same path as a
window close). shadPS4 registers no SIGTERM/SIGINT handler, so SIGTERM would
kill the process hard and leave read-write save mounts with their
`sce_sys/corrupted` marker in place; STOP must come first and SIGTERM is
only the escalation fallback.
"""

import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Optional

from .base import Emulator, base_launch_env

log = logging.getLogger(__name__)

ROM_ROOT = Path(os.environ.get("ROM_ROOT", "/romm"))
"""Library root ROM paths resolve under (env `ROM_ROOT`, default `/romm`)."""

XDG_DATA_HOME = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share")
"""The XDG data root, `~/.local/share` when `XDG_DATA_HOME` is unset."""
VERSIONS_DIR = Path(
    os.environ.get(
        "SHADPS4_VERSIONS_DIR",
        str(Path.home() / ".local/share/shadPS4QtLauncher/versions"),
    )
)
"""Where the launcher downloads builds, one folder per release (env `SHADPS4_VERSIONS_DIR`).

Defaults to `~/.local/share/shadPS4QtLauncher/versions`.
"""
DATA_DIR = Path(os.environ.get("SHADPS4_DATA_DIR", str(Path(XDG_DATA_HOME) / "shadPS4")))
"""shadPS4's data root holding save data (env `SHADPS4_DATA_DIR`, default `$XDG_DATA_HOME/shadPS4`)."""
SHADPS4_LOG_PATH = Path(os.environ.get("SHADPS4_LOG_PATH", "/config/shadps4.log"))
"""The emulator log file (env `SHADPS4_LOG_PATH`, default `/config/shadps4.log`)."""

ROM_EXTENSIONS = (".zar", ".bin")
"""Bootable formats: shadPS4 boots a game folder (eboot.bin inside it) or a .zar archive file."""

BIN_NAME = os.environ.get("SHADPS4_BIN_NAME", "Shadps4-sdl.AppImage")
"""The binary looked for inside a release folder (env `SHADPS4_BIN_NAME`, default `Shadps4-sdl.AppImage`).

Release folders look like `v0.17.0 - Garbage Collector's Edition - 2026-07-30`.
The `Pre-release` folder always carries the newest build and trumps all.
"""
_VERSION_RE = re.compile(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?")
"""Parses the semver prefix of a release folder name."""
_PRE_RELEASE_DIR = "pre-release"
"""Lowercased name of the pre-release folder, skipped by the semver scan."""


def _find_binary_in(folder: Path) -> Optional[Path]:
    """The shadPS4 binary inside one release folder.

    Args:
        folder: A release folder under `VERSIONS_DIR`.

    Returns:
        `BIN_NAME` when present, else the first `*.AppImage` by name, else None.
    """
    candidate = folder / BIN_NAME
    if candidate.is_file():
        return candidate
    for p in sorted(folder.glob("*.AppImage")):
        if p.is_file():
            return p
    return None


def _resolve_binary() -> Optional[Path]:
    """Latest shadps4 binary.

    The explicit `SHADPS4_BIN` override, else the Pre-release build if
    present, else the newest semver release folder.

    Returns:
        The binary path, or None (logged) when no usable build exists.
    """
    override = os.environ.get("SHADPS4_BIN")
    if override:
        return Path(override)
    if not VERSIONS_DIR.is_dir():
        log.warning("shadps4 versions dir not found: %s", VERSIONS_DIR)
        return None

    pre = _find_binary_in(VERSIONS_DIR / "Pre-release")
    if pre is not None:
        log.info("shadps4: using pre-release build %s", pre)
        return pre

    best: Optional[tuple[tuple[int, int, int], Path]] = None
    for folder in VERSIONS_DIR.iterdir():
        if not folder.is_dir() or folder.name.lower() == _PRE_RELEASE_DIR:
            continue
        binary = _find_binary_in(folder)
        if binary is None:
            continue
        m = _VERSION_RE.match(folder.name)
        if m is None:
            continue
        version = (int(m.group(1)), int(m.group(2) or 0), int(m.group(3) or 0))
        if best is None or version > best[0]:
            best = (version, binary)
    if best is not None:
        log.info("shadps4: using release %s (%s)", best[0], best[1])
        return best[1]
    log.warning("shadps4: no usable binary under %s", VERSIONS_DIR)
    return None


class Shadps4(Emulator):
    """PlayStation 4 via shadPS4, driven over its stdin IPC protocol.

    The binary is picked from the launcher's versions tree at launch time
    and spawned fullscreen with `SHADPS4_ENABLE_IPC=true` and a stdin pipe.
    RUN then START are written straight away so the game boots without
    waiting on the RUN deadline, and the stop writes STOP, which pushes
    SDL_EVENT_QUIT, the same path as a window close. shadPS4 registers no
    SIGTERM/SIGINT handler, so a bare SIGTERM would kill it hard and leave
    read-write save mounts with their `sce_sys/corrupted` marker in place;
    SIGTERM is only the escalation after STOP times out or the pipe breaks.

    There are no save states: persistence is the game's own save data under
    `home/1000/savedata`, keyed by game serial, which is what the archive
    carries. A resume slot is logged and ignored.

    Attributes:
        name: Provider key, `shadps4`.
        display_name: Human-readable name.
        save_root: The data directory the save subtree hangs off.
        save_subtrees: Save data plus its per-title param.sfo, under the default PS4 user.
        rom_extensions: Bootable formats.
        log_path: The emulator log file.
        term_timeout: Seconds STOP gets before SIGTERM (env `SHADPS4_STOP_WAIT`, default 20).
    """

    name = "shadps4"
    display_name = "shadPS4"
    save_root = DATA_DIR
    save_subtrees = ("home/1000/savedata",)
    """Save data plus its per-title param.sfo, under the default PS4 user."""
    rom_extensions = ROM_EXTENSIONS
    log_path = SHADPS4_LOG_PATH
    term_timeout = float(os.environ.get("SHADPS4_STOP_WAIT", "20"))
    """Seconds the IPC STOP gets before SIGTERM (env `SHADPS4_STOP_WAIT`, default 20).

    STOP goes through the SDL event loop into a graceful teardown; give it
    room before escalating to SIGTERM.
    """

    def resolve_rom_file(self, path: Path) -> Optional[Path]:
        """The path shadPS4 should boot for `path`.

        Args:
            path: A ROM file, or a game folder.

        Returns:
            The file itself, the folder's `eboot.bin`, the folder when it
            has none (shadPS4 appends eboot.bin to directory paths itself),
            or None when the path does not exist.
        """
        if path.is_file():
            return path
        if not path.is_dir():
            return None
        eboot = path / "eboot.bin"
        try:
            if eboot.is_file():
                if eboot.resolve().is_relative_to(ROM_ROOT):
                    return eboot
                return None  # symlink escapes ROM_ROOT
            if eboot.exists() or eboot.is_symlink():
                # present but not a regular file: dangling symlink, or a
                # symlink to a directory/device/fifo. is_file() misses these,
                # and falling through to `return path` would hand shadps4 an
                # unvalidated target via its own eboot.bin lookup.
                return None
        except OSError:
            return None
        return path  # shadps4 appends eboot.bin to directory paths itself

    def launch(self, rom_path: Path, resume_slot: Optional[int]) -> None:
        """Spawn the newest shadPS4 with IPC enabled and boot the game.

        Args:
            rom_path: The file or folder to boot.
            resume_slot: Ignored with a log line; shadPS4 has no save states.

        Raises:
            RuntimeError: When no binary is found under `VERSIONS_DIR`.
        """
        self.stop()
        if resume_slot is not None:
            log.info(
                "shadps4 has no save states, resume_slot %s ignored "
                "(game resumes from its own save data)",
                resume_slot,
            )
        binary = _resolve_binary()
        if binary is None:
            raise RuntimeError(f"no shadps4 binary found under {VERSIONS_DIR}")
        env = base_launch_env()
        env["SHADPS4_ENABLE_IPC"] = "true"
        log.info("launching shadps4 (rom=%s, binary=%s)", rom_path, binary)
        self._spawn([str(binary), "-f", "true", "-g", str(rom_path)], env, stdin_pipe=True)
        # The IPC input thread starts with the process and stdin buffers early
        # writes; RUN then START release the run/start semaphores so the game
        # boots without waiting on the 5 s RUN deadline.
        if not self._ipc_send("RUN"):
            log.warning("shadps4 IPC RUN failed, game may not boot until the 5 s RUN deadline")
        if not self._ipc_send("START"):
            log.warning("shadps4 IPC START failed, game may not boot")

    def _ipc_send(self, cmd: str) -> bool:
        """Write one IPC command line to the emulator's stdin.

        Args:
            cmd: The command, such as `RUN` or `START`; a newline is appended.

        Returns:
            True when the line was written and flushed, False when there is
            no live process with a stdin pipe or the pipe is broken.
        """
        proc = self._proc
        if proc is None or proc.stdin is None or proc.poll() is not None:
            return False
        try:
            proc.stdin.write(f"{cmd}\n".encode())
            proc.stdin.flush()
            return True
        except (BrokenPipeError, OSError):
            return False

    def stop(self) -> None:
        """Ask shadPS4 to quit over IPC, escalating to the base SIGTERM stop.

        STOP is written to stdin and the process given `term_timeout` to
        exit on its own; a broken pipe or a timeout falls through to the
        SIGTERM then SIGKILL sequence in the base class.
        """
        proc = self._proc
        if proc is not None and proc.poll() is None and proc.stdin is not None:
            log.info("stopping %s (pid %d) via IPC STOP", self.name, proc.pid)
            try:
                proc.stdin.write(b"STOP\n")
                proc.stdin.flush()
                proc.wait(timeout=self.term_timeout)
                self._forget()
                log.info("%s exited gracefully", self.name)
                return
            except (BrokenPipeError, OSError):
                pass
            except subprocess.TimeoutExpired:
                log.warning(
                    "%s did not exit after STOP, escalating to SIGTERM", self.name
                )
        super().stop()
