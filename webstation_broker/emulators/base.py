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
    # Whether the emulator can save and load state mid-session. Emulators whose
    # only persistence is the game's own save data leave this off, so the state
    # routes refuse instead of silently doing nothing.
    supports_states: bool = False
    # The one slot the broker saves into. RomM is the library of states: every
    # save is pulled out of the container and every stored state is pushed back
    # into this slot, so nothing here needs to address more than one. Requested
    # slots resolve to it rather than being honoured, which is why the routes
    # echo the effective slot back.
    state_slot: int = 0
    # Where that slot's file lives, for the state-file routes to read and write.
    state_dir: Path = Path("/config")
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

    def save_state(self, slot: int) -> bool:
        """Save the running game to `slot`. Only called when supports_states."""
        raise NotImplementedError

    def load_state(self, slot: int) -> bool:
        """Load `slot` into the running game. Only called when supports_states."""
        raise NotImplementedError

    def state_path(self) -> Path | None:
        """The file the working slot holds right now, or None if it is empty.

        This is what the state-file GET serves, so it has to be the file the
        emulator just wrote, not the newest state in the directory: another
        slot or another game's state would otherwise be filed in RomM as this
        save."""
        return None

    def state_screenshot_path(self) -> Path | None:
        """The frame captured alongside the working slot's state, or None.

        Only for emulators that write the thumbnail as a separate file. The
        ones that embed it in the state itself return None and let RomM pull it
        out of the state it already fetched."""
        return None

    def clear_working_slot(self) -> None:
        """Drop whatever the working slot holds from an earlier session.

        Called at activate, before the incoming save archive is restored, so
        only the container's own leftovers go. Emulators that name a state
        after the loaded content can tell a stale one apart on sight and leave
        this alone; the override exists for the ones that cannot."""

    def state_target(self, filename: str) -> Path | None:
        """Where a pushed state called `filename` belongs, or None if the name
        is not one this emulator would write for the loaded game.

        Validating the name against the emulator's own convention is what keeps
        a caller from dropping arbitrary files into the save tree. The slot in
        it is not part of that test: RomM holds the library, so a stored state
        carries whatever slot it was captured in and lands in this broker's own
        working slot regardless."""
        return None

    def wait_for_state(self, deadline: float, poll: float = 0.5) -> bool:
        """Block until the working slot holds a state file, or `deadline` passes.

        A resume state can turn up after launch: the state-file routes only
        answer while a session is up, so RomM pushes its pick once activate has
        returned and the game is already booting. Waiting for it here is what
        keeps a deferred resume load from firing on a slot that is still empty
        and reporting a fresh start."""
        while time.monotonic() < deadline:
            if self.state_path() is not None:
                return True
            time.sleep(poll)
        return self.state_path() is not None

    def save_and_exit(self, slot: int) -> dict:
        """Save state (best effort) and stop. Default: nothing to save."""
        self.stop()
        return {"state_saved": None, "state_slot": None, "state_file": None}

    def resolve_rom_file(self, path: Path) -> Path | None:
        """File the emulator should boot for `path` (folder or file)."""
        raise NotImplementedError
