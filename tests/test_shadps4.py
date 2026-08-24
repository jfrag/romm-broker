"""shadPS4 ROM resolution, binary version selection, launch, and IPC-driven stop."""

import os
import subprocess
from pathlib import Path
from typing import Optional

import pytest

from webstation_broker.emulators import base, shadps4


@pytest.fixture
def rom_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point shadps4's ROM_ROOT at a fresh temporary directory."""
    root = tmp_path / "romm"
    root.mkdir()
    monkeypatch.setattr(shadps4, "ROM_ROOT", root)
    return root


def test_resolve_refuses_an_eboot_that_symlinks_out_of_the_rom_root(
    rom_root: Path, tmp_path: Path
) -> None:
    """Resolution rejects an eboot symlink pointing outside the ROM root."""
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.bin"
    secret.write_bytes(b"not a game")
    folder = rom_root / "MyGame"
    folder.mkdir()
    (folder / "eboot.bin").symlink_to(secret)

    assert shadps4.Shadps4().resolve_rom_file(folder) is None


def test_resolve_accepts_an_eboot_that_symlinks_inside_the_rom_root(rom_root: Path) -> None:
    """Resolution accepts an eboot symlink that stays inside the ROM root."""
    shared = rom_root / "SharedAssets"
    shared.mkdir()
    real_eboot = shared / "actual_eboot.bin"
    real_eboot.write_bytes(b"game data")
    folder = rom_root / "MyGame"
    folder.mkdir()
    (folder / "eboot.bin").symlink_to(real_eboot)

    assert shadps4.Shadps4().resolve_rom_file(folder) == folder / "eboot.bin"


def test_resolve_refuses_a_dangling_eboot_symlink(rom_root: Path) -> None:
    """Resolution rejects an eboot symlink whose target does not exist."""
    folder = rom_root / "MyGame"
    folder.mkdir()
    (folder / "eboot.bin").symlink_to(rom_root / "does-not-exist")

    assert shadps4.Shadps4().resolve_rom_file(folder) is None


def test_resolve_refuses_an_eboot_that_symlinks_to_a_non_regular_file_outside_the_rom_root(
    rom_root: Path, tmp_path: Path
) -> None:
    """Resolution rejects an eboot symlink to a non-regular file outside the ROM root."""
    outside = tmp_path / "outside"
    outside.mkdir()
    os.mkfifo(outside / "pipe")
    folder = rom_root / "MyGame"
    folder.mkdir()
    (folder / "eboot.bin").symlink_to(outside / "pipe")

    assert shadps4.Shadps4().resolve_rom_file(folder) is None


def test_resolve_takes_a_direct_file_as_given(rom_root: Path) -> None:
    """Resolution returns a direct ROM file path unchanged."""
    rom = rom_root / "game.zar"
    rom.write_bytes(b"")

    assert shadps4.Shadps4().resolve_rom_file(rom) == rom


def test_resolve_finds_eboot_inside_a_game_folder(rom_root: Path) -> None:
    """Resolution finds the eboot file inside a game folder."""
    folder = rom_root / "MyGame"
    folder.mkdir()
    eboot = folder / "eboot.bin"
    eboot.write_bytes(b"")

    assert shadps4.Shadps4().resolve_rom_file(folder) == eboot


def test_resolve_falls_back_to_the_bare_folder_when_there_is_no_eboot(rom_root: Path) -> None:
    """Resolution falls back to the bare game folder when no eboot file exists."""
    folder = rom_root / "MyGame"
    folder.mkdir()

    assert shadps4.Shadps4().resolve_rom_file(folder) == folder


def test_resolve_returns_nothing_for_a_path_that_is_neither_file_nor_folder(
    rom_root: Path,
) -> None:
    """Resolution returns nothing for a path that is neither a file nor a folder."""
    missing = rom_root / "nope"

    assert shadps4.Shadps4().resolve_rom_file(missing) is None


@pytest.fixture
def versions_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point shadps4's VERSIONS_DIR at a fresh temporary directory."""
    d = tmp_path / "versions"
    d.mkdir()
    monkeypatch.setattr(shadps4, "VERSIONS_DIR", d)
    monkeypatch.delenv("SHADPS4_BIN", raising=False)
    return d


def _make_release(root: Path, folder_name: str, bin_name: str = "Shadps4-sdl.AppImage") -> Path:
    folder = root / folder_name
    folder.mkdir(parents=True)
    binary = folder / bin_name
    binary.write_bytes(b"")
    return binary


def test_an_explicit_override_wins_over_everything(
    versions_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The SHADPS4_BIN override wins over any discovered release folder."""
    _make_release(versions_dir, "v0.17.0 - Garbage Collector's Edition")
    monkeypatch.setenv("SHADPS4_BIN", "/opt/custom/shadps4")

    assert shadps4._resolve_binary() == Path("/opt/custom/shadps4")


def test_the_pre_release_folder_beats_every_numbered_release(versions_dir: Path) -> None:
    """The Pre-release folder is preferred over any numbered release."""
    _make_release(versions_dir, "v99.0.0 - Newest Looking Number")
    pre = _make_release(versions_dir, "Pre-release")

    assert shadps4._resolve_binary() == pre


def test_the_highest_semver_release_wins_by_number_not_by_string(versions_dir: Path) -> None:
    """Release selection compares versions numerically, not lexicographically."""
    _make_release(versions_dir, "v0.9.9 - A")
    newest = _make_release(versions_dir, "v0.9.10 - B")

    assert shadps4._resolve_binary() == newest


def test_release_folder_precedence_across_more_than_two_versions(versions_dir: Path) -> None:
    """Release selection picks the highest version among more than two candidates."""
    _make_release(versions_dir, "v0.9.9 - A")
    _make_release(versions_dir, "v0.9.10 - B")
    newest = _make_release(
        versions_dir, "v0.17.0 - Garbage Collector's Edition - 2026-07-30"
    )

    assert shadps4._resolve_binary() == newest


def test_a_folder_whose_name_does_not_parse_as_a_version_is_skipped_not_fatal(
    versions_dir: Path,
) -> None:
    """A folder name that fails to parse as a version is skipped, not fatal."""
    (versions_dir / "notes").mkdir()
    (versions_dir / "notes" / "Shadps4-sdl.AppImage").write_bytes(b"")
    newest = _make_release(versions_dir, "v0.5.0 - Only Real Release")

    assert shadps4._resolve_binary() == newest


def test_a_missing_versions_dir_resolves_to_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Binary resolution returns nothing when the versions directory is absent."""
    monkeypatch.setattr(shadps4, "VERSIONS_DIR", tmp_path / "does-not-exist")
    monkeypatch.delenv("SHADPS4_BIN", raising=False)

    assert shadps4._resolve_binary() is None


def test_a_versions_dir_with_no_usable_binary_resolves_to_nothing(versions_dir: Path) -> None:
    """Binary resolution returns nothing when no release folder has a binary."""
    (versions_dir / "v0.1.0 - Empty").mkdir()

    assert shadps4._resolve_binary() is None


class _FakeStdin:
    def __init__(self, fail: bool = False) -> None:
        self.written: list[bytes] = []
        self.flush_count = 0
        self.fail = fail

    def write(self, data: bytes) -> None:
        if self.fail:
            raise BrokenPipeError()
        self.written.append(data)

    def flush(self) -> None:
        self.flush_count += 1


class _FakeProc:
    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid
        self.stdin = _FakeStdin()
        self.wait_calls: list[Optional[float]] = []
        self.wait_exc: Optional[Exception] = None
        self.exit_code: Optional[int] = None

    def poll(self) -> Optional[int]:
        return self.exit_code

    def wait(self, timeout: Optional[float] = None) -> Optional[int]:
        self.wait_calls.append(timeout)
        if self.wait_exc is not None:
            raise self.wait_exc
        self.exit_code = 0
        return self.exit_code


def test_launch_stops_first_then_spawns_with_ipc_enabled(
    monkeypatch: pytest.MonkeyPatch, versions_dir: Path, rom_root: Path
) -> None:
    """Launch stops any running instance before spawning a new one with IPC enabled."""
    binary = _make_release(versions_dir, "v0.17.0 - Only Release")
    order = []
    monkeypatch.setattr(shadps4.Shadps4, "stop", lambda self: order.append("stop"))
    spawned = {}

    def fake_spawn(
        self: shadps4.Shadps4, cmd: list[str], env: dict[str, str], stdin_pipe: bool = False
    ) -> None:
        order.append("spawn")
        spawned["cmd"] = cmd
        spawned["env"] = env
        spawned["stdin_pipe"] = stdin_pipe
        self._proc = _FakeProc()

    monkeypatch.setattr(shadps4.Shadps4, "_spawn", fake_spawn)
    rom = rom_root / "game.zar"
    rom.write_bytes(b"")
    emu = shadps4.Shadps4()

    emu.launch(rom, resume_slot=None)

    assert order == ["stop", "spawn"]
    assert spawned["cmd"] == [str(binary), "-f", "true", "-g", str(rom)]
    assert spawned["env"]["SHADPS4_ENABLE_IPC"] == "true"
    assert spawned["stdin_pipe"] is True
    assert emu._proc.stdin.written == [b"RUN\n", b"START\n"]


def test_launch_logs_and_ignores_a_resume_slot(
    monkeypatch: pytest.MonkeyPatch,
    versions_dir: Path,
    rom_root: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Launch ignores an unsupported resume_slot and logs that it did so."""
    _make_release(versions_dir, "v0.17.0 - Only Release")
    monkeypatch.setattr(shadps4.Shadps4, "stop", lambda self: None)

    def fake_spawn(
        self: shadps4.Shadps4, cmd: list[str], env: dict[str, str], stdin_pipe: bool = False
    ) -> None:
        self._proc = _FakeProc()

    monkeypatch.setattr(shadps4.Shadps4, "_spawn", fake_spawn)
    rom = rom_root / "game.zar"
    rom.write_bytes(b"")
    emu = shadps4.Shadps4()

    with caplog.at_level("INFO"):
        emu.launch(rom, resume_slot=3)

    assert "resume_slot 3 ignored" in caplog.text
    assert emu._proc.stdin.written == [b"RUN\n", b"START\n"]


def test_launch_raises_when_no_binary_is_available(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, rom_root: Path
) -> None:
    """Launch raises when no shadPS4 binary can be resolved."""
    monkeypatch.setattr(shadps4, "VERSIONS_DIR", tmp_path / "does-not-exist")
    monkeypatch.delenv("SHADPS4_BIN", raising=False)
    monkeypatch.setattr(shadps4.Shadps4, "stop", lambda self: None)
    spawned = []
    monkeypatch.setattr(
        shadps4.Shadps4,
        "_spawn",
        lambda self, cmd, env, stdin_pipe=False: spawned.append(cmd),
    )
    rom = rom_root / "game.zar"
    rom.write_bytes(b"")
    emu = shadps4.Shadps4()

    with pytest.raises(RuntimeError):
        emu.launch(rom, resume_slot=None)

    assert spawned == []


def test_stop_sends_ipc_stop_and_waits_for_a_graceful_exit(
    monkeypatch: pytest.MonkeyPatch, pid_record: Path
) -> None:
    """Stop sends the IPC STOP command and waits for a graceful exit."""
    escalated = []
    monkeypatch.setattr(base.Emulator, "stop", lambda self: escalated.append(True))
    emu = shadps4.Shadps4()
    proc = _FakeProc()
    emu._proc = proc

    emu.stop()

    assert proc.stdin.written == [b"STOP\n"]
    assert proc.stdin.flush_count == 1
    assert proc.wait_calls == [emu.term_timeout]
    assert escalated == []
    assert emu._proc is None
    assert not pid_record.exists()


def test_stop_falls_back_to_sigterm_escalation_when_the_stdin_write_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stop escalates to SIGTERM when the IPC stdin write fails."""
    escalated = []
    monkeypatch.setattr(base.Emulator, "stop", lambda self: escalated.append(True))
    emu = shadps4.Shadps4()
    proc = _FakeProc()
    proc.stdin = _FakeStdin(fail=True)
    emu._proc = proc

    emu.stop()

    assert escalated == [True]


def test_stop_falls_back_to_sigterm_escalation_when_the_process_never_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stop escalates to SIGTERM when the process never exits after IPC STOP."""
    escalated = []
    monkeypatch.setattr(base.Emulator, "stop", lambda self: escalated.append(True))
    emu = shadps4.Shadps4()
    proc = _FakeProc()
    proc.wait_exc = subprocess.TimeoutExpired(cmd="shadps4", timeout=emu.term_timeout)
    emu._proc = proc

    emu.stop()

    assert proc.stdin.written == [b"STOP\n"]
    assert escalated == [True]


def test_stop_is_a_no_op_when_nothing_is_running() -> None:
    """Stop does nothing when no process is running."""
    emu = shadps4.Shadps4()
    emu._proc = None

    emu.stop()

    assert emu._proc is None


def test_stop_skips_ipc_and_escalates_when_the_process_already_exited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stop skips the IPC command and escalates when the process already exited."""
    escalated = []
    monkeypatch.setattr(base.Emulator, "stop", lambda self: escalated.append(True))
    emu = shadps4.Shadps4()
    proc = _FakeProc()
    proc.exit_code = 0
    emu._proc = proc

    emu.stop()

    assert proc.stdin.written == []
    assert escalated == [True]


def test_ipc_send_returns_false_when_the_process_already_exited() -> None:
    """IPC send returns False when the process has already exited."""
    emu = shadps4.Shadps4()
    proc = _FakeProc()
    proc.exit_code = 0
    emu._proc = proc

    assert emu._ipc_send("RUN") is False
    assert proc.stdin.written == []


def test_ipc_send_returns_false_when_the_write_fails() -> None:
    """IPC send returns False when writing to stdin raises."""
    emu = shadps4.Shadps4()
    proc = _FakeProc()
    proc.stdin = _FakeStdin(fail=True)
    emu._proc = proc

    assert emu._ipc_send("RUN") is False


def test_ipc_send_returns_false_when_there_is_no_stdin() -> None:
    """IPC send returns False when the process has no stdin pipe."""
    emu = shadps4.Shadps4()
    proc = _FakeProc()
    proc.stdin = None
    emu._proc = proc

    assert emu._ipc_send("RUN") is False
