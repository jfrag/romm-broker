"""Shared fixtures.

Every test runs against a broker whose on-disk locations are redirected into
tmp_path, so nothing here can touch a real container's config tree. The
emulator modules read their paths at import time into module globals, which is
why the redirect is a monkeypatch of those globals rather than of the env.
"""

import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, Optional

import pytest
from fastapi.testclient import TestClient

from webstation_broker import selkies, session, settings
from webstation_broker.app import create_app
from webstation_broker.emulators import base
from webstation_broker.emulators.base import Emulator

PREFIX = settings.PREFIX


@pytest.fixture(autouse=True)
def clean_session() -> Iterator[None]:
    """Reset the module-global session state on both sides of every test.

    Session state is module-global, so a leak between tests is a false pass.

    Yields:
        Nothing; the test runs between the two resets.
    """
    session.SESSION = None
    session.LAST_EXIT = None
    session.ROOM["controller"] = None
    session.ROOM["viewers"] = {}
    yield
    session.SESSION = None
    session.LAST_EXIT = None
    session.ROOM["controller"] = None
    session.ROOM["viewers"] = {}


@pytest.fixture(autouse=True)
def no_selkies(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the selkies control plane so activate never waits on a refused connection.

    Without it every activate waits on a refused connection to a selkies that is not running.

    Args:
        monkeypatch: Pytest's attribute patcher, undone when the test ends.
    """

    async def _push(_session: dict[str, Any]) -> bool:
        """Pretend the token map was pushed.

        Args:
            _session: The session whose tokens would have been pushed.

        Returns:
            Always True.
        """
        return True

    async def _clear() -> bool:
        """Pretend the token map was cleared.

        Returns:
            Always True.
        """
        return True

    monkeypatch.setattr(selkies, "push_tokens", _push)
    monkeypatch.setattr(selkies, "clear_tokens", _clear)


@pytest.fixture(autouse=True)
def pid_record(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the emulator pid record at tmp_path and hand back its path.

    Autouse because the app reaps whatever the record names at startup, and a developer box running
    the broker has a real one naming a real emulator.

    Args:
        monkeypatch: Pytest's attribute patcher, undone when the test ends.
        tmp_path: The per-test temporary directory.

    Returns:
        The path the pid record is written to.
    """
    path = tmp_path / "broker-emulator.json"
    monkeypatch.setattr(base, "PID_FILE", path)
    return path


@pytest.fixture
def sleeper() -> Iterator[Callable[[], subprocess.Popen[bytes]]]:
    """Hand out processes in their own session, each standing in for a running emulator.

    Every process spawned is killed and reaped when the test ends.

    Yields:
        A callable that spawns a sixty second sleep in a new session and returns it.
    """
    procs = []

    def spawn() -> subprocess.Popen[bytes]:
        """Start one sleeper and remember it for teardown.

        Returns:
            The spawned process.
        """
        proc = subprocess.Popen(["/usr/bin/sleep", "60"], start_new_session=True)
        procs.append(proc)
        return proc

    yield spawn
    for proc in procs:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


@pytest.fixture
def broker_dirs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Path]:
    """Point the ROM root and the archive directories at tmp_path.

    Args:
        monkeypatch: Pytest's attribute patcher, undone when the test ends.
        tmp_path: The per-test temporary directory.

    Returns:
        The created directories keyed as "roms", "exports" and "imports".
    """
    roms = tmp_path / "romm"
    exports = tmp_path / "exports"
    imports = tmp_path / "imports"
    for d in (roms, exports, imports):
        d.mkdir()
    monkeypatch.setattr(settings, "ROM_ROOT", roms)
    monkeypatch.setattr(settings, "EXPORT_DIR", exports)
    monkeypatch.setattr(settings, "IMPORT_DIR", imports)
    return {"roms": roms, "exports": exports, "imports": imports}


class FakeEmulator(Emulator):
    """An emulator that records calls instead of spawning anything.

    Stands in for a real Emulator subclass so the routes can be driven without a process behind
    them.

    Attributes:
        launched: The (rom_path, resume_slot) pair of the last launch, or None.
        cleared: Whether clear_working_slot was called.
        saved_slots: Every slot passed to save_state, in order.
        loaded_slots: Every slot passed to load_state, in order.
        exit_slots: Every slot passed to save_and_exit, in order.
        running: What alive reports; stop flips it off.
        state_file: The path state_path and state_target answer with.
        swapped_discs: Every path passed to swap_disc, in order.
        swap_ok: What swap_disc answers.
        launch_fails: Whether launch raises instead of recording the call.
    """

    name = "fake"
    display_name = "Fake"
    rom_extensions = (".iso",)
    supports_states = True
    supports_disc_swap = True
    state_slot = 3
    launch_fails = False

    def __init__(self) -> None:
        """Build the fake with nothing launched and every recorder empty."""
        super().__init__()
        self.launched = None
        self.cleared = False
        self.saved_slots = []
        self.loaded_slots = []
        self.exit_slots = []
        self.running = True
        self.state_file: Optional[Path] = None
        self.swapped_discs: list[Path] = []
        self.swap_ok = True

    def alive(self) -> bool:
        """Report whether the fake is still running.

        Returns:
            The value of the running flag.
        """
        return self.running

    def stop(self) -> None:
        """Mark the fake as no longer running."""
        self.running = False

    def clear_working_slot(self) -> None:
        """Record that the working slot was emptied."""
        self.cleared = True

    def resolve_rom_file(self, path: Path) -> Optional[Path]:
        """Pick the bootable file for a ROM path.

        Args:
            path: A ROM file, or a folder holding one.

        Returns:
            The path itself when it is a file, else the first file in the folder with
            a known extension, or None when there is none.
        """
        if path.is_file():
            return path
        candidates = sorted(p for p in path.glob("*") if p.suffix.lower() in self.rom_extensions)
        return candidates[0] if candidates else None

    def launch(self, rom_path: Optional[Path], resume_slot: Optional[int]) -> None:
        """Record the launch instead of spawning anything.

        Args:
            rom_path: The ROM handed to the emulator.
            resume_slot: The state slot to resume from, or None.

        Raises:
            RuntimeError: When launch_fails is set, so a test can drive the failure path.
        """
        if self.launch_fails:
            raise RuntimeError("fake launch failure")
        self.launched = (rom_path, resume_slot)

    def save_state(self, slot: int) -> bool:
        """Record a state save request.

        Args:
            slot: The slot the caller asked for.

        Returns:
            Always True.
        """
        self.saved_slots.append(slot)
        return True

    def load_state(self, slot: int) -> bool:
        """Record a state load request.

        Args:
            slot: The slot the caller asked for.

        Returns:
            Always True.
        """
        self.loaded_slots.append(slot)
        return True

    def state_path(self) -> Optional[Path]:
        """Report where the working slot's state lives.

        Returns:
            Whatever the test stored in state_file.
        """
        return self.state_file

    def state_target(self, filename: str) -> Optional[Path]:
        """Report where a pushed state would be written.

        Args:
            filename: The name RomM files the state under; ignored by the fake.

        Returns:
            Whatever the test stored in state_file.
        """
        return self.state_file

    def save_and_exit(self, slot: Optional[int]) -> dict[str, Any]:
        """Record the exit slot and stop.

        Args:
            slot: The slot to save to before exiting, or None to skip the state.

        Returns:
            The state report the exit route echoes: nothing saved for None, else the
            working slot.
        """
        self.exit_slots.append(slot)
        self.stop()
        if slot is None:
            return {"state_saved": False, "state_slot": None, "state_file": None}
        return {"state_saved": True, "state_slot": self.state_slot, "state_file": None}

    def swap_disc(self, path: Path) -> bool:
        """Record a disc swap request.

        Args:
            path: The disc image to mount.

        Returns:
            The value of swap_ok, so a test can make the swap fail.
        """
        self.swapped_discs.append(path)
        return self.swap_ok


@pytest.fixture
def fake_emulator(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[FakeEmulator]:
    """Register FakeEmulator under "fake" and hand back the list the registry appends to.

    The list is how a test reaches the instance the route is holding.

    save_root lands in tmp_path as well: the exit path dumps whatever is under it, and the class
    default is the real /config.

    Args:
        monkeypatch: Pytest's attribute patcher, undone when the test ends.
        tmp_path: The per-test temporary directory.

    Returns:
        The list every instance the registry builds is appended to.
    """
    from webstation_broker import emulators

    built = []
    root = tmp_path / "fakeconfig"
    (root / "saves").mkdir(parents=True)

    class Tracked(FakeEmulator):
        """A FakeEmulator rooted in tmp_path that appends each instance to the shared list."""

        save_root = root
        save_subtrees = ("saves",)

        def __init__(self) -> None:
            """Build the fake and append it to the shared list."""
            super().__init__()
            built.append(self)

    monkeypatch.setitem(emulators.REGISTRY, "fake", Tracked)
    return built


@pytest.fixture
def client(broker_dirs: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """Serve the app through a TestClient with no secret and dev mode off.

    Args:
        broker_dirs: The redirected ROM root and archive directories. Requested so they exist before
            the app starts.
        monkeypatch: Pytest's attribute patcher, undone when the test ends.

    Yields:
        A client whose app lifespan is running for the duration of the test.
    """
    monkeypatch.setattr(settings, "BROKER_SECRET", "")
    monkeypatch.setattr(settings, "DEV_MODE", False)
    with TestClient(create_app()) as c:
        yield c


@pytest.fixture
def secret_client(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Turn the broker secret on for an already running client.

    Args:
        client: The running test client.
        monkeypatch: Pytest's attribute patcher, undone when the test ends.

    Returns:
        The same client, now served by a broker that demands X-Broker-Secret.
    """
    monkeypatch.setattr(settings, "BROKER_SECRET", "s3cret")
    return client
