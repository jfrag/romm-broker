"""RPCS3 config/ipc patching, the savestates symlink, save_subtrees, resume
target selection, save-and-exit, and boot verification."""

import os
import socket
import struct
import threading
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


def test_clearing_the_working_slot_wipes_every_title_leftover_state(rpcs3_dirs):
    """A state left behind by a previous session must not outrank (by
    mtime) whatever this session's own archive restore brings back."""
    stale = _touch(rpcs3_dirs["sstate_root"] / "BLUS30443" / "BLUS30443_1.SAVESTAT")
    other_title = _touch(rpcs3_dirs["sstate_root"] / "OTHER00000" / "OTHER00000_1.SAVESTAT")

    rpcs3.Rpcs3().clear_working_slot()

    assert not stale.exists()
    assert not other_title.exists()
    assert rpcs3_dirs["sstate_root"].is_dir()


def test_clearing_the_working_slot_enters_restoring_mode(rpcs3_dirs):
    """api.py reads save_subtrees for the restore extract before it calls
    prepare_restore(), so clear_working_slot has to flip _restoring itself."""
    emu = rpcs3.Rpcs3()

    emu.clear_working_slot()

    assert emu._restoring is True
    assert emu.save_subtrees == ("home/00000001/savedata", "game", "savestates")


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


@pytest.mark.parametrize("ext", [".SAVESTAT", ".SAVESTAT.zst", ".SAVESTAT.gz"])
def test_newest_state_matches_every_extension_rpcs3_actually_writes(rpcs3_dirs, ext):
    state = _touch(rpcs3_dirs["sstate_root"] / "BLUS30443" / f"BLUS30443_1{ext}")

    assert rpcs3._newest_state("BLUS30443") == state


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


def test_changed_state_picks_the_newest_when_several_files_changed(rpcs3_dirs):
    """Must agree with _newest_state, which resume selection relies on, not
    just report whichever changed file the directory glob happens to yield
    first."""
    before = rpcs3._state_snapshot("BLUS30443")
    _touch(rpcs3_dirs["sstate_root"] / "BLUS30443" / "BLUS30443_1.SAVESTAT", mtime=1000)
    newest = _touch(
        rpcs3_dirs["sstate_root"] / "BLUS30443" / "BLUS30443_2.SAVESTAT", mtime=3000
    )

    assert rpcs3._changed_state("BLUS30443", before) == newest


# ── save state write confirmation ───────────────────────────────────────


def test_wait_for_state_write_returns_the_file_once_its_size_stops_changing(rpcs3_dirs):
    serial = "BLUS30443"
    before = rpcs3._state_snapshot(serial)
    target = _touch(rpcs3_dirs["sstate_root"] / serial / f"{serial}_1.SAVESTAT")

    result = rpcs3._wait_for_state_write(serial, before, time.monotonic() + 5.0)

    assert result == target


def test_wait_for_state_write_returns_none_when_the_file_never_stabilizes(rpcs3_dirs, monkeypatch):
    """A file that keeps growing past the deadline must be reported as
    unsaved, not handed back as if the write had finished."""
    monkeypatch.setattr(rpcs3.time, "sleep", lambda _seconds: None)
    serial = "BLUS30443"
    target = _touch(rpcs3_dirs["sstate_root"] / serial / f"{serial}_1.SAVESTAT")
    counter = {"n": 0}

    def growing_changed_state(_serial, _before):
        counter["n"] += 1
        target.write_bytes(b"x" * counter["n"])
        return target

    monkeypatch.setattr(rpcs3, "_changed_state", growing_changed_state)

    result = rpcs3._wait_for_state_write(serial, {}, time.monotonic() + 0.2)

    assert result is None
    assert counter["n"] > 1


def test_wait_for_state_write_returns_none_when_nothing_ever_appears(rpcs3_dirs, monkeypatch):
    monkeypatch.setattr(rpcs3.time, "sleep", lambda _seconds: None)

    result = rpcs3._wait_for_state_write("BLUS30443", {}, time.monotonic() + 0.1)

    assert result is None


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


def test_exit_does_not_abort_the_save_dump_when_the_state_file_disappears_before_stat(
    rpcs3_dirs, monkeypatch
):
    """_wait_for_state_write can hand back a path that's gone by the time we
    stat it (e.g. RPCS3 renames it during compression); that must not raise
    out of save_and_exit and skip the stop()/dump that follows."""
    emu = rpcs3.Rpcs3()
    emu._session_serial = "BLUS30443"
    monkeypatch.setattr(rpcs3.Rpcs3, "alive", lambda self: True)
    emu._send_key = lambda key: True
    missing = rpcs3_dirs["sstate_root"] / "BLUS30443" / "BLUS30443_1.SAVESTAT"
    monkeypatch.setattr(rpcs3, "_wait_for_state_write", lambda serial, before, deadline: missing)
    stopped = {"called": False}
    emu.stop = lambda: stopped.__setitem__("called", True)

    report = emu.save_and_exit(1)

    assert report == {"state_saved": False, "state_slot": 1, "state_file": None}
    assert stopped["called"] is True


# ── PINE wire protocol ────────────────────────────────────────────────────


@pytest.fixture
def pine_socket(monkeypatch, tmp_path):
    sock_path = tmp_path / "rpcs3.sock"
    monkeypatch.setattr(rpcs3, "PINE_SOCKET", sock_path)
    return sock_path


def _serve_pine_reply(sock_path: Path, reply: bytes | None):
    """Accepts one connection, reads and discards the request, then writes
    back `reply` (or nothing, to simulate a closed/no-reply connection)."""
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock_path))
    server.listen(1)

    def run():
        conn, _ = server.accept()
        with conn:
            conn.settimeout(5)
            header = conn.recv(5)
            if len(header) == 5:
                remaining = struct.unpack("<I", header[:4])[0] - 5
                got = 0
                while got < remaining:
                    chunk = conn.recv(remaining - got)
                    if not chunk:
                        break
                    got += len(chunk)
            if reply is not None:
                conn.sendall(reply)
        server.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def test_pine_request_returns_the_reply_body_on_success(pine_socket):
    body = b"status-body"
    reply = struct.pack("<IB", 5 + len(body), 0) + body
    thread = _serve_pine_reply(pine_socket, reply)

    result = rpcs3._pine_request(rpcs3._PINE_MSG_STATUS, timeout=2.0)
    thread.join(timeout=2)

    assert result == body


def test_pine_request_returns_none_on_a_nonzero_result_byte(pine_socket):
    reply = struct.pack("<IB", 5, 1)
    thread = _serve_pine_reply(pine_socket, reply)

    result = rpcs3._pine_request(rpcs3._PINE_MSG_STATUS, timeout=2.0)
    thread.join(timeout=2)

    assert result is None


def test_pine_request_returns_none_on_a_truncated_header(pine_socket):
    thread = _serve_pine_reply(pine_socket, b"\x05\x00")

    result = rpcs3._pine_request(rpcs3._PINE_MSG_STATUS, timeout=2.0)
    thread.join(timeout=2)

    assert result is None


def test_pine_request_coerces_a_truncated_body_to_empty(pine_socket):
    """The declared size promises 5 payload bytes but the connection closes
    after 2; _pine_recv_exact's None gets folded into b"" rather than
    propagated. Both current callers (_pine_status, _pine_title_id) treat an
    empty body the same as None, so this documents the actual coercion
    rather than asserting a failure path that doesn't exist."""
    reply = struct.pack("<IB", 10, 0) + b"ab"
    thread = _serve_pine_reply(pine_socket, reply)

    result = rpcs3._pine_request(rpcs3._PINE_MSG_STATUS, timeout=2.0)
    thread.join(timeout=2)

    assert result == b""


class _StubSocket:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    def recv(self, _n):
        return self._chunks.pop(0) if self._chunks else b""


def test_pine_recv_exact_accumulates_across_several_recv_calls():
    sock = _StubSocket([b"ab", b"cde", b"f"])

    assert rpcs3._pine_recv_exact(sock, 6) == b"abcdef"


def test_pine_recv_exact_returns_none_when_the_socket_closes_early():
    sock = _StubSocket([b"ab", b""])

    assert rpcs3._pine_recv_exact(sock, 6) is None


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
    # alive() forced True: the deadline path would also leave boot_failed
    # False on a real (unpatched) instance, so without this the test would
    # pass even if the "reported running" early return were deleted.
    monkeypatch.setattr(rpcs3.Rpcs3, "alive", lambda self: True)
    emu = rpcs3.Rpcs3()
    emu._session_serial = "BLUS30443"

    emu._boot_watchdog(emu._launch_seq)

    assert emu.boot_failed is False


def test_boot_watchdog_resolves_an_unknown_serial_once_running(
    rpcs3_dirs, monkeypatch, watchdog_env
):
    monkeypatch.setattr(rpcs3, "_pine_status", lambda: 0)
    monkeypatch.setattr(rpcs3, "_pine_title_id", lambda: "BLUS30443")
    monkeypatch.setattr(rpcs3.Rpcs3, "alive", lambda self: True)
    emu = rpcs3.Rpcs3()
    emu._session_serial = None

    emu._boot_watchdog(emu._launch_seq)

    assert emu._session_serial == "BLUS30443"
    assert emu.boot_failed is False


def test_boot_watchdog_discards_a_title_lookup_that_lands_after_a_supersede(
    rpcs3_dirs, monkeypatch, watchdog_env
):
    """_pine_title_id() can block past a relaunch/stop; the resolved title
    must not be stamped onto a session that has already moved on."""
    monkeypatch.setattr(rpcs3, "_pine_status", lambda: 0)
    emu = rpcs3.Rpcs3()
    seq = emu._launch_seq
    emu._session_serial = None

    def title_id():
        emu._launch_seq += 1  # a relaunch/stop landed during the PINE round trip
        return "BLUS30443"

    monkeypatch.setattr(rpcs3, "_pine_title_id", title_id)

    emu._boot_watchdog(seq)

    assert emu._session_serial is None


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


def test_boot_watchdog_abandons_a_superseded_launch(rpcs3_dirs, monkeypatch):
    # A step of 1.0 (vs watchdog_env's 30.0) keeps the loop under the 90s
    # deadline long enough to re-enter the body after the seq bump below,
    # which is what actually exercises the in-loop supersede check rather
    # than just falling through to the post-loop one.
    monkeypatch.setattr(rpcs3.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(rpcs3.time, "monotonic", _FakeClock(step=1.0))
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
    # Exactly 2: the 3rd loop iteration re-enters with the bumped seq and
    # returns from the in-loop check before calling _pine_status again.
    assert calls["n"] == 2


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
