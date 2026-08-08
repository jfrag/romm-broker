"""Emulator interface and shared launch plumbing."""

import logging
import os
import signal
import subprocess
import time
from pathlib import Path

log = logging.getLogger(__name__)

XDG_RUNTIME_DIR = os.environ.get("XDG_RUNTIME_DIR", "/config/.XDG")


def base_launch_env() -> dict[str, str]:
    """Environment apps are launched into: the broker's own environment,
    pointed at the running labwc session's displays."""
    env = dict(os.environ)
    env["WAYLAND_DISPLAY"] = os.environ.get("BROKER_WAYLAND_DISPLAY", "wayland-0")
    env["DISPLAY"] = os.environ.get("BROKER_DISPLAY", ":0")
    # s6 services get a minimal PATH; emulator binaries live in /usr/games.
    path = env.get("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
    for extra in ("/usr/local/bin", "/usr/bin", "/usr/games", "/usr/local/games"):
        if extra not in path.split(":"):
            path = f"{path}:{extra}"
    env["PATH"] = path
    return env


class Emulator:
    name: str = "base"
    display_name: str = "Webstation"
    requires_rom: bool = True
    # Root of the emulator's writable data and the subtrees under it that
    # hold save data; save restore and dump are scoped to these.
    save_root: Path = Path("/config")
    save_subtrees: tuple[str, ...] = ()
    rom_extensions: tuple[str, ...] = ()
    log_path: Path = Path("/config/broker-app.log")
    # Seconds SIGTERM gets before escalating to SIGKILL.
    term_timeout: float = 5.0

    def __init__(self):
        self._proc: subprocess.Popen | None = None

    def _spawn(self, cmd: list[str], env: dict[str, str], stdin_pipe: bool = False) -> None:
        """Start the app in its own process group with output captured.

        `stdin_pipe` keeps the child's stdin as a pipe so emulators with a
        stdin control protocol (shadPS4 IPC) can be driven headlessly.
        """
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
                stdin=subprocess.PIPE if stdin_pipe else None,
                stdout=log_fh if log_fh else subprocess.DEVNULL,
                stderr=subprocess.STDOUT if log_fh else subprocess.DEVNULL,
                start_new_session=True,
            )
        finally:
            if log_fh:
                log_fh.close()

    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def stop(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None or proc.poll() is not None:
            return
        log.info("stopping %s (pid %d)", self.name, proc.pid)
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGTERM)
            try:
                proc.wait(timeout=self.term_timeout)
            except subprocess.TimeoutExpired:
                os.killpg(pgid, signal.SIGKILL)
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    log.error("%s did not exit after SIGKILL", self.name)
        except ProcessLookupError:
            pass

    def prepare_restore(self) -> None:
        """Hook run before a save archive is extracted into save_root.
        Default: nothing. Override to clear anything that would block the
        restore: a process holding a save file open, or an existing file
        the newer-file guard would wrongly keep over the archived one."""

    def launch(self, rom_path: Path | None, resume_slot: int | None) -> None:
        raise NotImplementedError

    def save_and_exit(self, slot: int) -> dict:
        """Save state (best effort) and stop. Default: nothing to save."""
        self.stop()
        return {"state_saved": None, "state_slot": None, "state_file": None}

    def resolve_rom_file(self, path: Path) -> Path | None:
        """File the emulator should boot for `path` (folder or file)."""
        raise NotImplementedError
