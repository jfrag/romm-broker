"""RPCS3 config/ipc patching, the savestates symlink, save_subtrees, resume
target selection, save-and-exit, and boot verification."""

import os
import time
from pathlib import Path

import pytest

from webstation_broker.emulators import rpcs3


@pytest.fixture
def rpcs3_dirs(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    dev_hdd0 = data_dir / "dev_hdd0"
    user_home = dev_hdd0 / "home" / "00000001"
    game_dir = dev_hdd0 / "game"
    sstate_root = data_dir / "savestates"
    sstate_link = dev_hdd0 / "savestates"
    dev_hdd0.mkdir(parents=True)
    game_dir.mkdir()
    (user_home / "savedata").mkdir(parents=True)

    monkeypatch.setattr(rpcs3, "DATA_DIR", data_dir)
    monkeypatch.setattr(rpcs3, "CONFIG_PATH", data_dir / "config.yml")
    monkeypatch.setattr(rpcs3, "IPC_PATH", data_dir / "ipc.yml")
    monkeypatch.setattr(rpcs3, "DEV_HDD0", dev_hdd0)
    monkeypatch.setattr(rpcs3, "USER_HOME", user_home)
    monkeypatch.setattr(rpcs3, "EXDATA_DIR", user_home / "exdata")
    monkeypatch.setattr(rpcs3, "GAME_DIR", game_dir)
    monkeypatch.setattr(rpcs3, "SSTATE_ROOT", sstate_root)
    monkeypatch.setattr(rpcs3, "_SSTATE_LINK", sstate_link)
    monkeypatch.setattr(rpcs3.Rpcs3, "save_root", dev_hdd0)
    return {
        "data_dir": data_dir,
        "dev_hdd0": dev_hdd0,
        "user_home": user_home,
        "game_dir": game_dir,
        "sstate_root": sstate_root,
        "sstate_link": sstate_link,
    }


def _touch(path: Path, mtime=None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"state")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


# ── config.yml / ipc.yml patching ───────────────────────────────────────


def test_patch_config_seeds_a_missing_file_with_every_forced_key(rpcs3_dirs):
    rpcs3._patch_config()

    text = rpcs3.CONFIG_PATH.read_text()
    assert "Miscellaneous:" in text
    assert "  Automatically start games after boot: true" in text
    assert "  Exit RPCS3 when process finishes: true" in text
    assert "  Pause emulation on RPCS3 focus loss: false" in text


def test_patch_config_overwrites_a_conflicting_value_but_keeps_the_rest(rpcs3_dirs):
    rpcs3.CONFIG_PATH.write_text(
        "Miscellaneous:\n"
        "  Exit RPCS3 when process finishes: false\n"
        "  Some Other Setting: 5\n"
    )

    rpcs3._patch_config()

    text = rpcs3.CONFIG_PATH.read_text()
    assert "  Exit RPCS3 when process finishes: true" in text
    assert "  Some Other Setting: 5" in text


def test_patch_ipc_seeds_a_missing_file_as_a_flat_key(rpcs3_dirs):
    rpcs3._patch_ipc()

    assert rpcs3.IPC_PATH.read_text().strip() == "IPC Server enabled: true"


def test_patch_ipc_overwrites_an_existing_flat_key(rpcs3_dirs):
    rpcs3.IPC_PATH.write_text("IPC Server enabled: false\nIPC Port: 28080\n")

    rpcs3._patch_ipc()

    text = rpcs3.IPC_PATH.read_text()
    assert "IPC Server enabled: true" in text
    assert "IPC Port: 28080" in text


# ── savestates symlink ──────────────────────────────────────────────────


def test_ensure_sstate_link_creates_a_fresh_symlink(rpcs3_dirs):
    rpcs3._ensure_sstate_link()

    link = rpcs3_dirs["sstate_link"]
    assert link.is_symlink()
    assert link.resolve() == rpcs3_dirs["sstate_root"].resolve()


def test_ensure_sstate_link_is_idempotent(rpcs3_dirs):
    rpcs3._ensure_sstate_link()
    rpcs3._ensure_sstate_link()

    assert rpcs3_dirs["sstate_link"].resolve() == rpcs3_dirs["sstate_root"].resolve()


def test_ensure_sstate_link_repoints_a_symlink_at_the_wrong_target(rpcs3_dirs, tmp_path):
    wrong = tmp_path / "wrong"
    wrong.mkdir()
    rpcs3_dirs["sstate_link"].symlink_to(wrong, target_is_directory=True)

    rpcs3._ensure_sstate_link()

    assert rpcs3_dirs["sstate_link"].resolve() == rpcs3_dirs["sstate_root"].resolve()


def test_ensure_sstate_link_leaves_a_real_directory_alone(rpcs3_dirs):
    real = rpcs3_dirs["sstate_link"]
    real.mkdir()
    (real / "marker").write_text("do not touch")

    rpcs3._ensure_sstate_link()

    assert not real.is_symlink()
    assert (real / "marker").exists()


def test_clearing_the_working_slot_ensures_the_symlink(rpcs3_dirs):
    rpcs3.Rpcs3().clear_working_slot()

    assert rpcs3_dirs["sstate_link"].is_symlink()


def test_building_every_emulator_does_not_touch_the_symlink(rpcs3_dirs):
    """clear_working_slot, not __init__/save_subtrees, is what creates the
    link: a registry sweep constructs every emulator against real paths with
    no per-emulator redirect, so construction alone must stay read-only."""
    rpcs3.Rpcs3()

    assert not rpcs3_dirs["sstate_link"].exists()


# ── save_subtrees ───────────────────────────────────────────────────────


def test_save_subtrees_includes_savestates_when_dumping(rpcs3_dirs):
    emu = rpcs3.Rpcs3()

    assert "savestates" in emu.save_subtrees
    assert "home/00000001/savedata" in emu.save_subtrees


def test_save_subtrees_includes_savestates_when_restoring(rpcs3_dirs):
    emu = rpcs3.Rpcs3()
    emu._restoring = True

    assert emu.save_subtrees == ("home/00000001/savedata", "game", "savestates")


# ── state snapshot / diff ───────────────────────────────────────────────


def test_newest_state_reads_the_newest_file_for_the_title(rpcs3_dirs):
    _touch(rpcs3_dirs["sstate_root"] / "BLUS30443" / "BLUS30443_1.SAVESTAT", mtime=1000)
    newest = _touch(
        rpcs3_dirs["sstate_root"] / "BLUS30443" / "BLUS30443_2.SAVESTAT", mtime=3000
    )
    _touch(rpcs3_dirs["sstate_root"] / "OTHER00000" / "OTHER00000_9.SAVESTAT", mtime=9000)

    assert rpcs3._newest_state("BLUS30443") == newest


def test_newest_state_is_none_without_a_title_dir(rpcs3_dirs):
    assert rpcs3._newest_state("BLUS30443") is None
    assert rpcs3._newest_state(None) is None


def test_changed_state_finds_the_file_that_appeared(rpcs3_dirs):
    before = rpcs3._state_snapshot("BLUS30443")
    new = _touch(rpcs3_dirs["sstate_root"] / "BLUS30443" / "BLUS30443_1.SAVESTAT")

    assert rpcs3._changed_state("BLUS30443", before) == new


def test_changed_state_finds_a_rewritten_file_by_size(rpcs3_dirs):
    p = _touch(rpcs3_dirs["sstate_root"] / "BLUS30443" / "BLUS30443_1.SAVESTAT")
    before = rpcs3._state_snapshot("BLUS30443")
    p.write_bytes(b"a longer state than before")

    assert rpcs3._changed_state("BLUS30443", before) == p


def test_changed_state_is_none_when_nothing_moved(rpcs3_dirs):
    _touch(rpcs3_dirs["sstate_root"] / "BLUS30443" / "BLUS30443_1.SAVESTAT")
    before = rpcs3._state_snapshot("BLUS30443")

    assert rpcs3._changed_state("BLUS30443", before) is None


# ── launch() resume selection ───────────────────────────────────────────


@pytest.fixture
def no_boot_watchdog(monkeypatch):
    started = []

    def mock_thread(target, args, daemon):
        started.append((target.__name__, args))
        return type("MockThread", (), {"start": lambda s: None})()

    monkeypatch.setattr(rpcs3, "Thread", mock_thread)
    return started


def test_launch_boots_the_state_file_when_a_resume_is_found(
    rpcs3_dirs, no_boot_watchdog, monkeypatch, tmp_path
):
    monkeypatch.setattr(rpcs3, "_patch_config", lambda: None)
    monkeypatch.setattr(rpcs3, "_patch_ipc", lambda: None)
    spawned = {}
    monkeypatch.setattr(
        rpcs3.Rpcs3, "_spawn", lambda self, cmd, env: spawned.setdefault("cmd", cmd)
    )
    eboot = rpcs3_dirs["game_dir"] / "BLUS30443" / "USRDIR" / "EBOOT.BIN"
    _touch(eboot)
    state = _touch(rpcs3_dirs["sstate_root"] / "BLUS30443" / "BLUS30443_3.SAVESTAT")

    rpcs3.Rpcs3().launch(eboot, 1)

    assert spawned["cmd"][-1] == str(state)


def test_launch_falls_back_to_a_fresh_boot_with_no_resume_state(
    rpcs3_dirs, no_boot_watchdog, monkeypatch
):
    monkeypatch.setattr(rpcs3, "_patch_config", lambda: None)
    monkeypatch.setattr(rpcs3, "_patch_ipc", lambda: None)
    spawned = {}
    monkeypatch.setattr(
        rpcs3.Rpcs3, "_spawn", lambda self, cmd, env: spawned.setdefault("cmd", cmd)
    )
    eboot = rpcs3_dirs["game_dir"] / "BLUS30443" / "USRDIR" / "EBOOT.BIN"
    _touch(eboot)

    rpcs3.Rpcs3().launch(eboot, 1)

    assert spawned["cmd"][-1] == str(eboot)


def test_launch_falls_back_to_a_fresh_boot_with_no_known_serial(
    rpcs3_dirs, no_boot_watchdog, monkeypatch, tmp_path
):
    monkeypatch.setattr(rpcs3, "_patch_config", lambda: None)
    monkeypatch.setattr(rpcs3, "_patch_ipc", lambda: None)
    spawned = {}
    monkeypatch.setattr(
        rpcs3.Rpcs3, "_spawn", lambda self, cmd, env: spawned.setdefault("cmd", cmd)
    )
    iso = tmp_path / "Game.iso"
    iso.write_bytes(b"iso")

    rpcs3.Rpcs3().launch(iso, 1)

    assert spawned["cmd"][-1] == str(iso)


def test_launch_boots_normally_without_a_resume_slot(rpcs3_dirs, no_boot_watchdog, monkeypatch):
    monkeypatch.setattr(rpcs3, "_patch_config", lambda: None)
    monkeypatch.setattr(rpcs3, "_patch_ipc", lambda: None)
    spawned = {}
    monkeypatch.setattr(
        rpcs3.Rpcs3, "_spawn", lambda self, cmd, env: spawned.setdefault("cmd", cmd)
    )
    eboot = rpcs3_dirs["game_dir"] / "BLUS30443" / "USRDIR" / "EBOOT.BIN"
    _touch(eboot)
    _touch(rpcs3_dirs["sstate_root"] / "BLUS30443" / "BLUS30443_3.SAVESTAT")

    rpcs3.Rpcs3().launch(eboot, None)

    assert spawned["cmd"][-1] == str(eboot)


def test_launch_always_spawns_the_boot_watchdog(rpcs3_dirs, no_boot_watchdog, monkeypatch):
    monkeypatch.setattr(rpcs3, "_patch_config", lambda: None)
    monkeypatch.setattr(rpcs3, "_patch_ipc", lambda: None)
    monkeypatch.setattr(rpcs3.Rpcs3, "_spawn", lambda self, cmd, env: None)
    eboot = rpcs3_dirs["game_dir"] / "BLUS30443" / "USRDIR" / "EBOOT.BIN"
    _touch(eboot)

    emu = rpcs3.Rpcs3()
    emu.launch(eboot, None)

    assert len(no_boot_watchdog) == 1
    assert no_boot_watchdog[0][0] == "_boot_watchdog"
    assert no_boot_watchdog[0][1] == (emu._launch_seq,)


# ── save_and_exit ───────────────────────────────────────────────────────


def test_exit_without_a_slot_saves_nothing(rpcs3_dirs, monkeypatch):
    emu = rpcs3.Rpcs3()
    monkeypatch.setattr(rpcs3.Rpcs3, "alive", lambda self: True)
    emu._send_key = lambda key: pytest.fail("hotkey sent with no slot requested")

    report = emu.save_and_exit(None)

    assert report == {"state_saved": False, "state_slot": None, "state_file": None}


def test_exit_with_a_slot_but_no_known_serial_saves_nothing(rpcs3_dirs, monkeypatch):
    emu = rpcs3.Rpcs3()
    monkeypatch.setattr(rpcs3.Rpcs3, "alive", lambda self: True)
    emu._session_serial = None
    emu._send_key = lambda key: pytest.fail("hotkey sent with no known title id")

    report = emu.save_and_exit(1)

    assert report == {"state_saved": False, "state_slot": 1, "state_file": None}


def test_exit_reports_the_state_the_hotkey_wrote(rpcs3_dirs, monkeypatch):
    emu = rpcs3.Rpcs3()
    emu._session_serial = "BLUS30443"
    monkeypatch.setattr(rpcs3.Rpcs3, "alive", lambda self: True)

    written = rpcs3_dirs["sstate_root"] / "BLUS30443" / "BLUS30443_1.SAVESTAT"

    def fake_send_key(key):
        _touch(written)
        return True

    emu._send_key = fake_send_key

    report = emu.save_and_exit(1)

    assert report["state_saved"] is True
    assert report["state_slot"] == 1
    assert report["state_file"]["path"] == str(written)


def test_exit_reports_no_save_when_the_hotkey_fails_to_send(rpcs3_dirs, monkeypatch):
    emu = rpcs3.Rpcs3()
    emu._session_serial = "BLUS30443"
    monkeypatch.setattr(rpcs3.Rpcs3, "alive", lambda self: True)
    emu._send_key = lambda key: False

    report = emu.save_and_exit(1)

    assert report == {"state_saved": False, "state_slot": 1, "state_file": None}


def test_exit_reports_no_save_when_no_new_state_appears(rpcs3_dirs, monkeypatch):
    emu = rpcs3.Rpcs3()
    emu._session_serial = "BLUS30443"
    monkeypatch.setattr(rpcs3.Rpcs3, "alive", lambda self: True)
    emu._send_key = lambda key: True
    monkeypatch.setattr(rpcs3, "_wait_for_state_write", lambda serial, before, deadline: None)

    report = emu.save_and_exit(1)

    assert report == {"state_saved": False, "state_slot": 1, "state_file": None}


# ── boot watchdog ───────────────────────────────────────────────────────


class _FakeClock:
    def __init__(self, step: float = 30.0):
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


@pytest.fixture
def watchdog_env(monkeypatch):
    monkeypatch.setattr(rpcs3.time, "sleep", lambda _seconds: None)
    clock = _FakeClock()
    monkeypatch.setattr(rpcs3.time, "monotonic", clock)
    return clock


def test_boot_watchdog_clears_the_flag_when_the_game_reports_running(
    rpcs3_dirs, monkeypatch, watchdog_env
):
    monkeypatch.setattr(rpcs3, "_pine_status", lambda: 0)
    emu = rpcs3.Rpcs3()
    emu._session_serial = "BLUS30443"

    emu._boot_watchdog(emu._launch_seq)

    assert emu.boot_failed is False


def test_boot_watchdog_resolves_an_unknown_serial_once_running(
    rpcs3_dirs, monkeypatch, watchdog_env
):
    monkeypatch.setattr(rpcs3, "_pine_status", lambda: 0)
    monkeypatch.setattr(rpcs3, "_pine_title_id", lambda: "BLUS30443")
    emu = rpcs3.Rpcs3()
    emu._session_serial = None

    emu._boot_watchdog(emu._launch_seq)

    assert emu._session_serial == "BLUS30443"
    assert emu.boot_failed is False


def test_boot_watchdog_flags_a_hang_when_the_process_is_still_alive(
    rpcs3_dirs, monkeypatch, watchdog_env
):
    monkeypatch.setattr(rpcs3, "_pine_status", lambda: None)
    monkeypatch.setattr(rpcs3.Rpcs3, "alive", lambda self: True)
    emu = rpcs3.Rpcs3()

    emu._boot_watchdog(emu._launch_seq)

    assert emu.boot_failed is True


def test_boot_watchdog_does_not_flag_a_process_that_already_exited(
    rpcs3_dirs, monkeypatch, watchdog_env
):
    monkeypatch.setattr(rpcs3, "_pine_status", lambda: None)
    monkeypatch.setattr(rpcs3.Rpcs3, "alive", lambda self: False)
    emu = rpcs3.Rpcs3()

    emu._boot_watchdog(emu._launch_seq)

    assert emu.boot_failed is False


def test_boot_watchdog_abandons_a_superseded_launch(rpcs3_dirs, monkeypatch, watchdog_env):
    emu = rpcs3.Rpcs3()
    seq = emu._launch_seq
    calls = {"n": 0}

    def status():
        calls["n"] += 1
        if calls["n"] == 2:
            emu._launch_seq += 1
        return None

    monkeypatch.setattr(rpcs3, "_pine_status", status)
    monkeypatch.setattr(rpcs3.Rpcs3, "alive", lambda self: True)

    emu._boot_watchdog(seq)

    assert emu.boot_failed is False


def test_stop_invalidates_an_in_flight_boot_watchdog(rpcs3_dirs):
    emu = rpcs3.Rpcs3()
    seq_before = emu._launch_seq

    emu.stop()

    assert emu._launch_seq != seq_before


# ── xdotool window targeting ────────────────────────────────────────────


def test_send_key_activates_then_sends_the_key(rpcs3_dirs):
    emu = rpcs3.Rpcs3()
    calls = []
    emu._xdotool = lambda *args: (calls.append(args), "111\n")[1] if args[0] == "search" else (
        calls.append(args), "ok"
    )[1]

    assert emu._send_key("ctrl+alt+1") is True
    assert calls[0] == ("search", "--class", rpcs3._WINDOW_CLASS)
    assert calls[1][0] == "windowactivate"
    assert calls[2] == ("key", "--clearmodifiers", "ctrl+alt+1")


def test_send_key_fails_with_no_window(rpcs3_dirs):
    emu = rpcs3.Rpcs3()
    emu._xdotool = lambda *args: None

    assert emu._send_key("ctrl+alt+1") is False
