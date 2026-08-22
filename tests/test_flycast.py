"""Flycast ROM resolution, transient -config composition, launch, and exit
via a graceful close request."""

import subprocess
from pathlib import Path

import pytest

from webstation_broker.emulators import base, flycast


@pytest.fixture
def rom_root(monkeypatch, tmp_path):
    root = tmp_path / "romm"
    root.mkdir()
    monkeypatch.setattr(flycast, "ROM_ROOT", root)
    return root


@pytest.fixture
def data_dir(monkeypatch, tmp_path):
    d = tmp_path / "data" / "flycast"
    d.mkdir(parents=True)
    monkeypatch.setattr(flycast, "DATA_DIR", d)
    monkeypatch.setattr(flycast.Flycast, "save_root", d.parent)
    monkeypatch.setattr(flycast.Flycast, "save_subtrees", (d.name,))
    return d


# ── resolve_rom_file / _pick_rom_file ───────────────────────────────────


def test_resolve_takes_a_direct_file_as_given(rom_root):
    rom = rom_root / "game.chd"
    rom.write_bytes(b"")

    assert flycast.Flycast().resolve_rom_file(rom) == rom


def test_resolve_returns_nothing_for_a_path_that_is_neither_file_nor_folder(rom_root):
    missing = rom_root / "nope"

    assert flycast.Flycast().resolve_rom_file(missing) is None


def test_resolve_prefers_chd_over_a_raw_gdi_beside_it(rom_root):
    folder = rom_root / "MyGame"
    folder.mkdir()
    (folder / "MyGame.gdi").write_bytes(b"")
    chd = folder / "MyGame.chd"
    chd.write_bytes(b"")

    assert flycast.Flycast().resolve_rom_file(folder) == chd


def test_resolve_prefers_disc_1_over_disc_2_at_the_same_extension_rank(rom_root):
    folder = rom_root / "MyGame"
    folder.mkdir()
    disc1 = folder / "MyGame (Disc 1).cdi"
    disc1.write_bytes(b"")
    (folder / "MyGame (Disc 2).cdi").write_bytes(b"")

    assert flycast.Flycast().resolve_rom_file(folder) == disc1


def test_resolve_ignores_dotfiles(rom_root):
    folder = rom_root / "MyGame"
    folder.mkdir()
    (folder / ".hidden.chd").write_bytes(b"")

    assert flycast.Flycast().resolve_rom_file(folder) is None


def test_resolve_ignores_extensions_it_does_not_recognize(rom_root):
    folder = rom_root / "MyGame"
    folder.mkdir()
    (folder / "readme.txt").write_bytes(b"")

    assert flycast.Flycast().resolve_rom_file(folder) is None


def test_resolve_accepts_a_homebrew_elf(rom_root):
    folder = rom_root / "MyHomebrew"
    folder.mkdir()
    elf = folder / "MyHomebrew.elf"
    elf.write_bytes(b"")

    assert flycast.Flycast().resolve_rom_file(folder) == elf


def test_resolve_refuses_a_disc_image_that_symlinks_outside_the_rom_root(rom_root, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.chd"
    secret.write_bytes(b"not a game")
    folder = rom_root / "MyGame"
    folder.mkdir()
    (folder / "MyGame.chd").symlink_to(secret)

    assert flycast.Flycast().resolve_rom_file(folder) is None


def test_resolve_accepts_a_disc_image_that_symlinks_inside_the_rom_root(rom_root):
    shared = rom_root / "SharedAssets"
    shared.mkdir()
    real = shared / "actual.chd"
    real.write_bytes(b"game data")
    folder = rom_root / "MyGame"
    folder.mkdir()
    link = folder / "MyGame.chd"
    link.symlink_to(real)

    assert flycast.Flycast().resolve_rom_file(folder) == real


def test_resolve_searches_one_level_of_subfolders(rom_root):
    folder = rom_root / "MyGame"
    sub = folder / "disc"
    sub.mkdir(parents=True)
    rom = sub / "MyGame.chd"
    rom.write_bytes(b"")

    assert flycast.Flycast().resolve_rom_file(folder) == rom


# ── launch ───────────────────────────────────────────────────────────────


def test_launch_stops_then_spawns(data_dir, rom_root, monkeypatch):
    order = []
    monkeypatch.setattr(flycast.Flycast, "stop", lambda self: order.append("stop"))

    def fake_spawn(self, cmd, env, stdin_pipe=False):
        order.append("spawn")

    monkeypatch.setattr(flycast.Flycast, "_spawn", fake_spawn)
    rom = rom_root / "game.chd"
    rom.write_bytes(b"")

    flycast.Flycast().launch(rom, resume_slot=None)

    assert order == ["stop", "spawn"]


def test_launch_with_no_resume_slot_omits_autoloadstate(data_dir, rom_root, monkeypatch):
    monkeypatch.setattr(flycast.Flycast, "stop", lambda self: None)
    spawned = {}

    def fake_spawn(self, cmd, env, stdin_pipe=False):
        spawned["cmd"] = cmd

    monkeypatch.setattr(flycast.Flycast, "_spawn", fake_spawn)
    rom = rom_root / "game.chd"
    rom.write_bytes(b"")

    flycast.Flycast().launch(rom, resume_slot=None)

    assert spawned["cmd"] == [
        flycast.FLYCAST_BIN,
        "-config",
        "config:Dreamcast.AutoSaveState=yes,config:Dreamcast.SavestateSlot=0,"
        "window:fullscreen=yes",
        str(rom),
    ]


def test_launch_with_a_resume_slot_and_a_state_file_enables_autoloadstate(
    data_dir, rom_root, monkeypatch
):
    monkeypatch.setattr(flycast.Flycast, "stop", lambda self: None)
    spawned = {}

    def fake_spawn(self, cmd, env, stdin_pipe=False):
        spawned["cmd"] = cmd

    monkeypatch.setattr(flycast.Flycast, "_spawn", fake_spawn)
    rom = rom_root / "game.chd"
    rom.write_bytes(b"")
    (data_dir / "game.state").write_bytes(b"state")

    flycast.Flycast().launch(rom, resume_slot=1)

    cmd = spawned["cmd"]
    assert "config:Dreamcast.AutoLoadState=yes" in cmd[2]


def test_launch_with_a_resume_slot_but_no_state_boots_fresh(
    data_dir, rom_root, monkeypatch, caplog
):
    monkeypatch.setattr(flycast.Flycast, "stop", lambda self: None)
    spawned = {}

    def fake_spawn(self, cmd, env, stdin_pipe=False):
        spawned["cmd"] = cmd

    monkeypatch.setattr(flycast.Flycast, "_spawn", fake_spawn)
    rom = rom_root / "game.chd"
    rom.write_bytes(b"")

    with caplog.at_level("WARNING"):
        flycast.Flycast().launch(rom, resume_slot=1)

    assert "config:Dreamcast.AutoLoadState=yes" not in spawned["cmd"][2]
    assert "resume requested but no resume state" in caplog.text


# ── _window (title match confirmed by owning pid) ──────────────────────────


class _FakeProc:
    def __init__(self, pid: int = 4242):
        self.pid = pid
        self.returncode = None
        self.wait_calls: list[float | None] = []
        self.wait_exc: Exception | None = None
        self.exit_code: int | None = None

    def poll(self):
        return self.exit_code

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        if self.wait_exc is not None:
            raise self.wait_exc
        self.exit_code = 0
        self.returncode = 0
        return self.exit_code


def test_window_is_none_when_nothing_is_running():
    emu = flycast.Flycast()
    emu._proc = None

    assert emu._window() is None


def test_window_is_none_when_the_title_search_fails(monkeypatch):
    emu = flycast.Flycast()
    emu._proc = _FakeProc()
    monkeypatch.setattr(flycast.Flycast, "_xdotool", lambda self, *a: None)

    assert emu._window() is None


def test_window_returns_the_id_whose_pid_matches_the_launched_process(monkeypatch):
    emu = flycast.Flycast()
    emu._proc = _FakeProc(pid=4242)
    calls = []

    def fake_xdotool(self, *args):
        calls.append(args)
        if args[0] == "search":
            return "111\n222\n"
        if args == ("getwindowpid", "111"):
            return "9999\n"
        if args == ("getwindowpid", "222"):
            return "4242\n"
        raise AssertionError(f"unexpected xdotool call: {args}")

    monkeypatch.setattr(flycast.Flycast, "_xdotool", fake_xdotool)

    assert emu._window() == "222"


def test_window_ignores_a_title_match_owned_by_a_different_pid(monkeypatch, caplog):
    emu = flycast.Flycast()
    emu._proc = _FakeProc(pid=4242)

    def fake_xdotool(self, *args):
        if args[0] == "search":
            return "111\n"
        if args == ("getwindowpid", "111"):
            return "9999\n"
        raise AssertionError(f"unexpected xdotool call: {args}")

    monkeypatch.setattr(flycast.Flycast, "_xdotool", fake_xdotool)

    with caplog.at_level("WARNING"):
        assert emu._window() is None

    assert "no flycast window found for pid 4242" in caplog.text


# ── stop (Alt+F4 close request, then SIGTERM escalation) ───────────────────


def test_stop_activates_the_window_and_sends_alt_f4_then_waits_for_exit(monkeypatch, pid_record):
    escalated = []
    monkeypatch.setattr(base.Emulator, "stop", lambda self: escalated.append(True))
    emu = flycast.Flycast()
    proc = _FakeProc()
    emu._proc = proc
    monkeypatch.setattr(emu, "_window", lambda: "12345")
    calls = []

    def fake_xdotool(self, *args):
        calls.append(args)
        return ""

    monkeypatch.setattr(flycast.Flycast, "_xdotool", fake_xdotool)

    emu.stop()

    assert calls == [
        ("windowactivate", "--sync", "12345"),
        ("key", "--clearmodifiers", "alt+F4"),
    ]
    assert proc.wait_calls == [emu.term_timeout]
    assert escalated == []
    assert emu._proc is None
    assert not pid_record.exists()


def test_stop_falls_back_to_sigterm_when_no_window_is_found(monkeypatch):
    escalated = []
    monkeypatch.setattr(base.Emulator, "stop", lambda self: escalated.append(True))
    emu = flycast.Flycast()
    emu._proc = _FakeProc()
    monkeypatch.setattr(emu, "_window", lambda: None)

    emu.stop()

    assert escalated == [True]


def test_stop_falls_back_to_sigterm_when_the_activate_fails(monkeypatch):
    escalated = []
    monkeypatch.setattr(base.Emulator, "stop", lambda self: escalated.append(True))
    emu = flycast.Flycast()
    emu._proc = _FakeProc()
    monkeypatch.setattr(emu, "_window", lambda: "12345")
    calls = []

    def fake_xdotool(self, *args):
        calls.append(args)
        return None if args[0] == "windowactivate" else ""

    monkeypatch.setattr(flycast.Flycast, "_xdotool", fake_xdotool)

    emu.stop()

    assert calls == [("windowactivate", "--sync", "12345")]
    assert escalated == [True]


def test_stop_falls_back_to_sigterm_when_the_process_never_exits(monkeypatch):
    escalated = []
    monkeypatch.setattr(base.Emulator, "stop", lambda self: escalated.append(True))
    emu = flycast.Flycast()
    proc = _FakeProc()
    proc.wait_exc = subprocess.TimeoutExpired(cmd="flycast", timeout=emu.term_timeout)
    emu._proc = proc
    monkeypatch.setattr(emu, "_window", lambda: "12345")
    monkeypatch.setattr(flycast.Flycast, "_xdotool", lambda self, *a: "")

    emu.stop()

    assert escalated == [True]


def test_stop_is_a_no_op_when_nothing_is_running():
    emu = flycast.Flycast()
    emu._proc = None

    emu.stop()

    assert emu._proc is None


def test_stop_skips_the_close_request_and_escalates_when_the_process_already_exited(monkeypatch):
    escalated = []
    monkeypatch.setattr(base.Emulator, "stop", lambda self: escalated.append(True))
    emu = flycast.Flycast()
    proc = _FakeProc()
    proc.exit_code = 0
    emu._proc = proc
    window_calls = []
    monkeypatch.setattr(emu, "_window", lambda: window_calls.append(True))

    emu.stop()

    assert window_calls == []
    assert escalated == [True]


# ── save_and_exit ────────────────────────────────────────────────────────


def _touch(path: Path, content: bytes = b"state") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_exit_without_a_slot_reports_nothing_but_still_stops(data_dir, monkeypatch):
    stopped = []
    monkeypatch.setattr(flycast.Flycast, "stop", lambda self: stopped.append(True))
    emu = flycast.Flycast()
    monkeypatch.setattr(emu, "alive", lambda: True)

    report = emu.save_and_exit(None)

    assert report == {"state_saved": False, "state_slot": None, "state_file": None}
    assert stopped == [True]


def test_exit_with_a_slot_reports_the_state_the_shutdown_wrote(data_dir, rom_root, monkeypatch):
    rom = rom_root / "game.chd"
    rom.write_bytes(b"")
    state = data_dir / "game.state"

    def fake_stop(self):
        _touch(state, b"a fresh state")

    monkeypatch.setattr(flycast.Flycast, "stop", fake_stop)
    emu = flycast.Flycast()
    emu._rom_path = rom
    emu._proc = _FakeProc()
    emu._proc.returncode = 0
    monkeypatch.setattr(emu, "alive", lambda: True)

    report = emu.save_and_exit(1)

    assert report["state_saved"] is True
    assert report["state_slot"] == 1
    assert report["state_file"]["path"] == str(state)


def test_exit_with_a_slot_reports_no_save_when_the_state_is_unchanged(
    data_dir, rom_root, monkeypatch, caplog
):
    rom = rom_root / "game.chd"
    rom.write_bytes(b"")
    state = _touch(data_dir / "game.state", b"stale, never rewritten")

    monkeypatch.setattr(flycast.Flycast, "stop", lambda self: None)
    emu = flycast.Flycast()
    emu._rom_path = rom
    emu._proc = _FakeProc()
    emu._proc.returncode = 0
    monkeypatch.setattr(emu, "alive", lambda: True)

    with caplog.at_level("WARNING"):
        report = emu.save_and_exit(1)

    assert report == {"state_saved": False, "state_slot": 1, "state_file": None}
    assert "unchanged" in caplog.text
    assert state.exists()  # never discarded: a graceful exit, just no rewrite


def test_exit_with_a_slot_discards_a_changed_state_killed_by_bare_sigterm(
    data_dir, rom_root, monkeypatch, caplog
):
    """Flycast has no SIGTERM handler, so the base class's SIGTERM
    escalation already ends it before dc_exit() runs, same as SIGKILL
    would; a state file that changed during that kill must not be
    reported as this exit's save."""
    rom = rom_root / "game.chd"
    rom.write_bytes(b"")
    state = data_dir / "game.state"

    def fake_stop(self):
        _touch(state)

    monkeypatch.setattr(flycast.Flycast, "stop", fake_stop)
    emu = flycast.Flycast()
    emu._rom_path = rom
    emu._proc = _FakeProc()
    emu._proc.returncode = -15  # -signal.SIGTERM
    monkeypatch.setattr(emu, "alive", lambda: True)

    with caplog.at_level("WARNING"):
        report = emu.save_and_exit(1)

    assert report == {"state_saved": False, "state_slot": 1, "state_file": None}
    assert not state.exists()
    assert "force-killed" in caplog.text
    assert "discarded untrusted resume state" in caplog.text


def test_exit_with_a_slot_discards_a_changed_state_killed_by_sigkill(
    data_dir, rom_root, monkeypatch, caplog
):
    rom = rom_root / "game.chd"
    rom.write_bytes(b"")
    state = data_dir / "game.state"

    def fake_stop(self):
        # A close-request escalation to SIGKILL could still land after
        # dc_exit() wrote a complete state, or mid-write; either way the
        # broker cannot tell torn from complete, so a file that changed
        # during the kill is never trusted.
        _touch(state)

    monkeypatch.setattr(flycast.Flycast, "stop", fake_stop)
    emu = flycast.Flycast()
    emu._rom_path = rom
    emu._proc = _FakeProc()
    emu._proc.returncode = -9  # -signal.SIGKILL
    monkeypatch.setattr(emu, "alive", lambda: True)

    with caplog.at_level("WARNING"):
        report = emu.save_and_exit(1)

    assert report == {"state_saved": False, "state_slot": 1, "state_file": None}
    assert not state.exists()
    assert "force-killed" in caplog.text
    assert "discarded untrusted resume state" in caplog.text


def test_exit_with_a_slot_leaves_an_unchanged_state_alone_when_force_killed(
    data_dir, rom_root, monkeypatch, caplog
):
    rom = rom_root / "game.chd"
    rom.write_bytes(b"")
    state = _touch(data_dir / "game.state", b"from an earlier session")

    monkeypatch.setattr(flycast.Flycast, "stop", lambda self: None)
    emu = flycast.Flycast()
    emu._rom_path = rom
    emu._proc = _FakeProc()
    emu._proc.returncode = -9  # -signal.SIGKILL
    monkeypatch.setattr(emu, "alive", lambda: True)

    with caplog.at_level("WARNING"):
        report = emu.save_and_exit(1)

    assert report == {"state_saved": False, "state_slot": 1, "state_file": None}
    assert state.exists()  # unchanged by the kill, so nothing to discard
    assert "force-killed" in caplog.text


def test_exit_with_a_slot_discards_a_changed_state_when_stop_never_confirms_the_exit(
    data_dir, rom_root, monkeypatch
):
    rom = rom_root / "game.chd"
    rom.write_bytes(b"")
    state = data_dir / "game.state"

    def fake_stop(self):
        _touch(state)

    monkeypatch.setattr(flycast.Flycast, "stop", fake_stop)
    emu = flycast.Flycast()
    emu._rom_path = rom
    emu._proc = _FakeProc()
    emu._proc.returncode = None
    monkeypatch.setattr(emu, "alive", lambda: True)

    report = emu.save_and_exit(1)

    assert report == {"state_saved": False, "state_slot": 1, "state_file": None}
    assert not state.exists()


def test_exit_with_a_slot_but_no_rom_loaded_warns_and_reports_nothing(
    data_dir, monkeypatch, caplog
):
    monkeypatch.setattr(flycast.Flycast, "stop", lambda self: None)
    emu = flycast.Flycast()
    emu._rom_path = None
    monkeypatch.setattr(emu, "alive", lambda: True)

    with caplog.at_level("WARNING"):
        report = emu.save_and_exit(1)

    assert report == {"state_saved": False, "state_slot": 1, "state_file": None}
    assert "no rom is currently loaded" in caplog.text


def test_exit_with_a_slot_but_not_alive_reports_nothing(data_dir, rom_root, monkeypatch):
    rom = rom_root / "game.chd"
    rom.write_bytes(b"")
    _touch(data_dir / "game.state")
    monkeypatch.setattr(flycast.Flycast, "stop", lambda self: None)
    emu = flycast.Flycast()
    emu._rom_path = rom
    monkeypatch.setattr(emu, "alive", lambda: False)

    report = emu.save_and_exit(1)

    assert report == {"state_saved": False, "state_slot": 1, "state_file": None}


# ── clear_working_slot ──────────────────────────────────────────────────


def test_clear_working_slot_is_a_noop_without_a_data_dir(tmp_path, monkeypatch):
    missing = tmp_path / "data" / "flycast"
    monkeypatch.setattr(flycast, "DATA_DIR", missing)

    flycast.Flycast().clear_working_slot()  # must not raise

    assert not missing.exists()


def test_clear_working_slot_wipes_every_leftover_resume_state(data_dir):
    """A resume state left behind by an earlier local session must not
    outrank (by mtime) whatever this session's own archive restore brings
    back, since the filename is only unambiguous once the rom is known,
    and clear_working_slot runs before that."""
    stale_a = _touch(data_dir / "game.state")
    stale_b = _touch(data_dir / "other.state")

    flycast.Flycast().clear_working_slot()

    assert not stale_a.exists()
    assert not stale_b.exists()
    assert data_dir.is_dir()


def test_clear_working_slot_leaves_unrelated_files_alone(data_dir):
    vmu = _touch(data_dir / "vmu_save_A1.bin")

    flycast.Flycast().clear_working_slot()

    assert vmu.exists()


def test_clear_working_slot_tolerates_a_file_it_cannot_delete(data_dir, monkeypatch, caplog):
    stuck = _touch(data_dir / "game.state")

    def boom(self):
        raise OSError("busy")

    monkeypatch.setattr(Path, "unlink", boom)

    with caplog.at_level("WARNING"):
        flycast.Flycast().clear_working_slot()  # must not raise

    assert stuck.exists()
    assert "could not clear stale resume state" in caplog.text


# ── class attributes (API surface parity with the other exit-only emulators) ──


def test_class_attributes_match_the_exit_only_api_surface(data_dir):
    emu = flycast.Flycast()

    assert emu.rom_extensions == (".chd", ".gdi", ".cdi", ".cue", ".elf")
    assert emu.supports_states is False
    assert emu.supports_disc_swap is False
