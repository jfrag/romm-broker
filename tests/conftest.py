"""Shared fixtures.

Every test runs against a broker whose on-disk locations are redirected into
tmp_path, so nothing here can touch a real container's config tree. The
emulator modules read their paths at import time into module globals, which is
why the redirect is a monkeypatch of those globals rather than of the env.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from webstation_broker import selkies, session, settings
from webstation_broker.app import create_app
from webstation_broker.emulators.base import Emulator

PREFIX = settings.PREFIX


@pytest.fixture(autouse=True)
def clean_session():
    """Session state is module-global, so a leak between tests is a false pass."""
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
def no_selkies(monkeypatch):
    """Stub the control plane: without it every activate waits on a refused
    connection to a selkies that is not running."""

    async def _push(_session):
        return True

    async def _clear():
        return True

    monkeypatch.setattr(selkies, "push_tokens", _push)
    monkeypatch.setattr(selkies, "clear_tokens", _clear)


@pytest.fixture
def broker_dirs(monkeypatch, tmp_path):
    """Point the ROM root and the archive directories at tmp_path."""
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
    """An emulator that records calls instead of spawning anything."""

    name = "fake"
    display_name = "Fake"
    rom_extensions = (".iso",)
    supports_states = True
    state_slot = 3

    def __init__(self):
        super().__init__()
        self.launched = None
        self.cleared = False
        self.saved_slots = []
        self.loaded_slots = []
        self.running = True
        self.state_file: Path | None = None

    def alive(self) -> bool:
        return self.running

    def stop(self) -> None:
        self.running = False

    def clear_working_slot(self) -> None:
        self.cleared = True

    def resolve_rom_file(self, path: Path) -> Path | None:
        if path.is_file():
            return path
        candidates = sorted(p for p in path.glob("*") if p.suffix.lower() in self.rom_extensions)
        return candidates[0] if candidates else None

    def launch(self, rom_path, resume_slot) -> None:
        self.launched = (rom_path, resume_slot)

    def save_state(self, slot: int) -> bool:
        self.saved_slots.append(slot)
        return True

    def load_state(self, slot: int) -> bool:
        self.loaded_slots.append(slot)
        return True

    def state_path(self) -> Path | None:
        return self.state_file

    def state_target(self, filename: str) -> Path | None:
        return self.state_file

    def save_and_exit(self, slot: int) -> dict:
        self.stop()
        return {"state_saved": True, "state_slot": self.state_slot, "state_file": None}


@pytest.fixture
def fake_emulator(monkeypatch, tmp_path):
    """Register FakeEmulator under "fake" and hand back the list the registry
    appends to, so a test can reach the instance the route is holding.

    save_root lands in tmp_path as well: the exit path dumps whatever is under
    it, and the class default is the real /config.
    """
    from webstation_broker import emulators

    built = []
    root = tmp_path / "fakeconfig"
    (root / "saves").mkdir(parents=True)

    class Tracked(FakeEmulator):
        save_root = root
        save_subtrees = ("saves",)

        def __init__(self):
            super().__init__()
            built.append(self)

    monkeypatch.setitem(emulators.REGISTRY, "fake", Tracked)
    return built


@pytest.fixture
def client(broker_dirs, monkeypatch):
    monkeypatch.setattr(settings, "BROKER_SECRET", "")
    monkeypatch.setattr(settings, "DEV_MODE", False)
    with TestClient(create_app()) as c:
        yield c


@pytest.fixture
def secret_client(client, monkeypatch):
    monkeypatch.setattr(settings, "BROKER_SECRET", "s3cret")
    return client
