"""Emulator interface and shared launch plumbing."""

import json
import logging
import os
import signal
import subprocess
import time
from pathlib import Path

log = logging.getLogger(__name__)

XDG_RUNTIME_DIR = os.environ.get("XDG_RUNTIME_DIR", "/config/.XDG")

# Where the running emulator's pid is recorded, so it can still be killed by a
# broker process that never spawned it. Emulators are started in their own
# session, so nothing else ties them to the broker: a broker restart (an s6
# bounce, or uvicorn --reload picking up an edit mid-session) otherwise leaves
# one playing with no handle on it, and the next launch stacks a second
# emulator on top of the first.
PID_FILE = Path(os.environ.get("BROKER_PID_FILE", "/config/broker-emulator.json"))

# Explicitly named broker secrets, plus a suffix pattern for anything shaped
# like one, stripped from every spawned emulator's environment. RetroArch in
# particular dlopen()s third-party cores with no sandboxing; a compromised
# core inheriting these could impersonate the broker's own API client.
_SENSITIVE_ENV_VARS = {"BROKER_SECRET", "SELKIES_MASTER_TOKEN", "GITHUB_TOKEN"}
_SENSITIVE_ENV_SUFFIXES = ("_SECRET", "_TOKEN", "_PASSWORD", "_KEY")


def base_launch_env() -> dict[str, str]:
    """Environment apps are launched into: the broker's own environment,
    pointed at the running labwc session's displays, with secret-shaped
    variables stripped out (see _SENSITIVE_ENV_VARS)."""
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in _SENSITIVE_ENV_VARS and not k.endswith(_SENSITIVE_ENV_SUFFIXES)
    }
    env["WAYLAND_DISPLAY"] = os.environ.get("BROKER_WAYLAND_DISPLAY", "wayland-0")
    env["DISPLAY"] = os.environ.get("BROKER_DISPLAY", ":0")
    # s6 services get a minimal PATH; emulator binaries live in /usr/games.
    path = env.get("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
    for extra in ("/usr/local/bin", "/usr/bin", "/usr/games", "/usr/local/games"):
        if extra not in path.split(":"):
            path = f"{path}:{extra}"
    env["PATH"] = path
    return env


def _record_pid(name: str, pid: int, cmd: list[str]) -> None:
    # Written through a temp file: the record exists for the case where the
    # broker dies, and a broker that dies mid-write would otherwise leave
    # half a line of JSON and no way to find the emulator it left behind.
    tmp = PID_FILE.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps({"name": name, "pid": pid, "cmd": cmd}))
        tmp.replace(PID_FILE)
    except OSError as exc:
        log.warning("could not record emulator pid: %s", exc)


def _clear_pid_record() -> None:
    try:
        PID_FILE.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("could not clear emulator pid record: %s", exc)


def _cmdline(pid: int) -> list[str]:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    return [part for part in raw.decode(errors="replace").split("\0") if part]


def reap_orphan() -> dict | None:
    """Kill an emulator left running by an earlier broker process.

    Only ever kills the pid the broker itself recorded, and only while that pid
    is still running the command it was recorded with, so a recycled pid is
    left alone. Returns what was killed, or None.
    """
    try:
        record = json.loads(PID_FILE.read_text())
    except (FileNotFoundError, ValueError):
        return None
    except OSError as exc:
        log.warning("could not read emulator pid record: %s", exc)
        return None

    pid, cmd = record.get("pid"), record.get("cmd")
    # An empty cmd would match the empty cmdline every dead pid reports, so a
    # record that cannot identify its process is thrown away rather than acted
    # on: the pid may belong to something else entirely by now.
    if not isinstance(pid, int) or not cmd or _cmdline(pid) != cmd:
        _clear_pid_record()
        return None

    try:
        # Emulators are spawned with start_new_session, so the recorded pid is
        # its own session and group leader. A pid that is not one is not the
        # process that was recorded, whatever its cmdline says.
        pgid = os.getpgid(pid)
        if pgid != pid or _cmdline(pid) != cmd:
            _clear_pid_record()
            return None

        log.warning("reaping orphaned %s (pid %d) from an earlier broker process",
                    record.get("name", "emulator"), pid)
        os.killpg(pgid, signal.SIGTERM)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and _cmdline(pid) == cmd:
            time.sleep(0.2)
        if _cmdline(pid) == cmd:
            os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError) as exc:
        log.warning("could not kill orphaned pid %d: %s", pid, exc)
    _clear_pid_record()
    return record


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
    # Whether the emulator can change the mounted disc without restarting.
    # Off by default so the swap route refuses instead of silently doing
    # nothing on an emulator that has no tray.
    supports_disc_swap: bool = False
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
    # The save subtree holding the whole memory card, for emulators that have
    # one. With whole-card sync on, that subtree travels on the memory-card
    # routes instead of inside the save archive, so activate drops it from the
    # restore and exit drops it from the dump.
    memory_card_subtree: str | None = None
    # A file the emulator looks for inside the card directory before it will
    # treat that directory as a card at all. The broker lays it down empty, the
    # way the emulator does when it creates a card itself; a card holding
    # nothing but this is still an empty slot.
    memory_card_marker: str | None = None

    def __init__(self):
        self._proc: subprocess.Popen | None = None
        # Set by an emulator that can tell its process is alive but never
        # reached a running game (the boot-error-dialog case). Passive signal
        # only: the broker surfaces it and takes no action of its own.
        self.boot_failed: bool = False

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
        _record_pid(self.name, self._proc.pid, cmd)

    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _forget(self) -> None:
        """Drop the handle on the emulator and the record of it on disk.

        Every path that ends with the process gone has to go through here.
        A graceful exit that only clears `_proc` leaves a record pointing at a
        pid nobody owns, and the next broker start would hunt it."""
        self._proc = None
        _clear_pid_record()

    def stop(self) -> None:
        proc = self._proc
        self._forget()
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

    def memory_card_path(self, platform: str | None = None) -> Path | None:
        """The directory holding the card the memory-card routes sync, or None.

        The broker names the card rather than reading the name out of the
        emulator's own config, because RomM lays a card down before the first
        launch has written that config. `platform` is the ROM's platform slug,
        for an emulator whose card exists on only one of several platforms it
        serves (GameCube vs Wii on Dolphin); most emulators ignore it."""
        return None

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

    def save_and_exit(self, slot: int | None) -> dict:
        """Save state (best effort) and stop. Default: nothing to save.

        A `slot` of None is an exit that writes no state. The game's own save
        data is still flushed and shipped: not writing a state is the whole of
        "exit without saving", and discarding an in-game save the player made
        at a save point would be losing real progress."""
        self.stop()
        return {"state_saved": None, "state_slot": None, "state_file": None}

    def resolve_rom_file(self, path: Path) -> Path | None:
        """File the emulator should boot for `path` (folder or file)."""
        raise NotImplementedError

    def swap_disc(self, path: Path) -> bool:
        """Mount `path` in place of the running disc. Only called when
        supports_disc_swap. True once the new disc is in the tray."""
        raise NotImplementedError
