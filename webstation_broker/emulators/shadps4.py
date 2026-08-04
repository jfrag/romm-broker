"""shadPS4 (PlayStation 4) launcher: release binary selection, ROM
resolution, and IPC-driven graceful shutdown.

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

from .base import Emulator, base_launch_env

log = logging.getLogger(__name__)

ROM_ROOT = Path(os.environ.get("ROM_ROOT", "/romm"))

XDG_DATA_HOME = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share")
# The launcher downloads builds into this tree, one folder per release.
VERSIONS_DIR = Path(
    os.environ.get(
        "SHADPS4_VERSIONS_DIR",
        str(Path.home() / ".local/share/shadPS4QtLauncher/versions"),
    )
)
DATA_DIR = Path(os.environ.get("SHADPS4_DATA_DIR", str(Path(XDG_DATA_HOME) / "shadPS4")))
SHADPS4_LOG_PATH = Path(os.environ.get("SHADPS4_LOG_PATH", "/config/shadps4.log"))

# shadPS4 boots a game folder (eboot.bin inside it) or a .zar archive file.
ROM_EXTENSIONS = (".zar", ".bin")

# Release folders look like `v0.17.0 - Garbage Collector's Edition - 2026-07-30`.
# The `Pre-release` folder always carries the newest build and trumps all.
BIN_NAME = os.environ.get("SHADPS4_BIN_NAME", "Shadps4-sdl.AppImage")
_VERSION_RE = re.compile(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?")
_PRE_RELEASE_DIR = "pre-release"


def _find_binary_in(folder: Path) -> Path | None:
    candidate = folder / BIN_NAME
    if candidate.is_file():
        return candidate
    for p in sorted(folder.glob("*.AppImage")):
        if p.is_file():
            return p
    return None


def _resolve_binary() -> Path | None:
    """Latest shadps4 binary: explicit override, else the Pre-release build
    if present, else the newest semver release folder."""
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

    best: tuple[tuple[int, int, int], Path] | None = None
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
    name = "shadps4"
    display_name = "shadPS4"
    save_root = DATA_DIR
    # Save data plus its per-title param.sfo, under the default PS4 user.
    save_subtrees = ("home/1000/savedata",)
    rom_extensions = ROM_EXTENSIONS
    log_path = SHADPS4_LOG_PATH
    # STOP goes through the SDL event loop into a graceful teardown; give it
    # room before escalating to SIGTERM.
    term_timeout = float(os.environ.get("SHADPS4_STOP_WAIT", "20"))

    def resolve_rom_file(self, path: Path) -> Path | None:
        if path.is_file():
            return path
        if not path.is_dir():
            return None
        eboot = path / "eboot.bin"
        if eboot.is_file():
            return eboot
        return path  # shadps4 appends eboot.bin to directory paths itself

    def launch(self, rom_path: Path, resume_slot: int | None) -> None:
        self.stop()
        if resume_slot:
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
        self._ipc_send("RUN")
        self._ipc_send("START")

    def _ipc_send(self, cmd: str) -> bool:
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
        proc = self._proc
        if proc is not None and proc.poll() is None and proc.stdin is not None:
            log.info("stopping %s (pid %d) via IPC STOP", self.name, proc.pid)
            try:
                proc.stdin.write(b"STOP\n")
                proc.stdin.flush()
                proc.wait(timeout=self.term_timeout)
                self._proc = None
                log.info("%s exited gracefully", self.name)
                return
            except (BrokenPipeError, OSError):
                pass
            except subprocess.TimeoutExpired:
                log.warning(
                    "%s did not exit after STOP, escalating to SIGTERM", self.name
                )
        super().stop()
