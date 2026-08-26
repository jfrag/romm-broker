"""shadPS4 ROM resolution, binary version selection, launch, and IPC-driven stop."""

import os
import subprocess
import threading
import time
import zipfile
from pathlib import Path
from typing import NoReturn, Optional

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


# ── pkg extraction / extraction cache ──────────────────────────────────


def _touch(path: Path, mtime: Optional[float] = None) -> Path:
    """Write a placeholder file, creating parents, optionally with a fixed mtime."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"state")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


@pytest.fixture
def cache_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the pkg extraction cache at an isolated temp directory with caching disabled."""
    cache = tmp_path / "cache"
    monkeypatch.setattr(shadps4, "CACHE_DIR", cache)
    monkeypatch.setattr(shadps4, "CACHE_ENABLED", False)
    return cache


@pytest.mark.parametrize("setting,expected", [
    ("1", True), ("true", True), ("YES", True), (" on ", True),
    ("0", False), ("false", False), ("", False),
])
def test_the_cache_enabled_switch_reads_the_usual_spellings(setting: str, expected: bool) -> None:
    """_truthy recognizes the usual truthy and falsy string spellings."""
    assert shadps4._truthy(setting) is expected


def test_extracted_dir_size_sums_files_and_skips_the_marker(cache_dir: Path) -> None:
    """Extracted dir size sums files and skips the last-accessed marker."""
    game_dir = cache_dir / "Game"
    _touch(game_dir / "eboot.bin")
    _touch(game_dir / "sce_sys" / "param.sfo")
    _touch(game_dir / shadps4._LAST_ACCESSED_MARKER)

    assert shadps4._extracted_dir_size(game_dir) == 10


def test_cache_size_bytes_sums_across_every_game_dir(cache_dir: Path) -> None:
    """Cache size bytes sums across every game dir."""
    _touch(cache_dir / "GameA" / "eboot.bin")
    _touch(cache_dir / "GameB" / "eboot.bin")

    assert shadps4._cache_size_bytes() == 10


def test_cache_size_bytes_is_zero_without_a_cache_dir(cache_dir: Path) -> None:
    """Cache size bytes is zero without a cache dir."""
    assert shadps4._cache_size_bytes() == 0


def test_touch_last_accessed_writes_a_marker_file(cache_dir: Path) -> None:
    """Touch last accessed writes a marker file."""
    game_dir = cache_dir / "Game"
    _touch(game_dir / "eboot.bin")

    shadps4._touch_last_accessed(game_dir)

    assert (game_dir / shadps4._LAST_ACCESSED_MARKER).exists()


def test_evict_lru_is_a_noop_when_disabled(cache_dir: Path) -> None:
    """Evict LRU is a no-op when disabled."""
    game_dir = cache_dir / "GameA"
    _touch(game_dir / "eboot.bin")

    shadps4._evict_lru(10**9, "SomethingElse")

    assert game_dir.exists()


def test_evict_lru_removes_the_least_recently_used_entry_first(
    cache_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Evict LRU removes the least recently used entry first."""
    monkeypatch.setattr(shadps4, "CACHE_ENABLED", True)
    monkeypatch.setattr(shadps4, "CACHE_MAX_GB", 8 / 1024**3)
    old = cache_dir / "Old"
    new = cache_dir / "New"
    _touch(old / "eboot.bin")
    _touch(new / "eboot.bin")
    _touch(old / shadps4._LAST_ACCESSED_MARKER, mtime=1000)
    _touch(new / shadps4._LAST_ACCESSED_MARKER, mtime=2000)

    shadps4._evict_lru(2, "Incoming")

    assert not old.exists()
    assert new.exists()


def test_evict_lru_never_removes_the_entry_being_extracted(
    cache_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Evict LRU never removes the entry being extracted."""
    monkeypatch.setattr(shadps4, "CACHE_ENABLED", True)
    monkeypatch.setattr(shadps4, "CACHE_MAX_GB", 1 / 1024**3)
    keep = cache_dir / "Incoming"
    _touch(keep / "eboot.bin")
    _touch(keep / shadps4._LAST_ACCESSED_MARKER, mtime=1)

    shadps4._evict_lru(50, "Incoming")

    assert keep.exists()


def test_evict_lru_gives_up_and_proceeds_when_nothing_is_left_to_evict(
    cache_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Evict LRU gives up and proceeds when nothing is left to evict."""
    monkeypatch.setattr(shadps4, "CACHE_ENABLED", True)
    monkeypatch.setattr(shadps4, "CACHE_MAX_GB", 1 / 1024**3)
    cache_dir.mkdir(parents=True, exist_ok=True)

    shadps4._evict_lru(10**9, "Incoming")  # must not raise


def test_extracted_boot_target_finds_the_eboot_pkg_extractor_produced(cache_dir: Path) -> None:
    """Extracted boot target finds the eboot.bin under a title-id output subfolder."""
    root = cache_dir / "Game"
    eboot = _touch(root / "CUSA23079" / "eboot.bin")

    assert shadps4._extracted_boot_target(root) == eboot


def test_extracted_boot_target_is_none_when_nothing_bootable_is_present(cache_dir: Path) -> None:
    """Extracted boot target is none when nothing bootable is present."""
    root = cache_dir / "Game"
    _touch(root / "CUSA23079" / "sce_sys" / "param.sfo")

    assert shadps4._extracted_boot_target(root) is None


def test_extracted_boot_target_rejects_an_eboot_that_symlinks_outside_root(
    cache_dir: Path, tmp_path: Path
) -> None:
    """Extracted boot target rejects an eboot symlinked outside the extraction root."""
    outside = _touch(tmp_path / "outside" / "eboot.bin")
    root = cache_dir / "Game"
    root.mkdir(parents=True)
    (root / "eboot.bin").symlink_to(outside)

    assert shadps4._extracted_boot_target(root) is None


def test_cache_key_changes_when_a_same_named_pkg_is_replaced(tmp_path: Path) -> None:
    """Cache key changes when a same-named pkg is replaced with different content."""
    pkg = tmp_path / "Game.pkg"
    pkg.write_bytes(b"original")
    original_key = shadps4._cache_key(pkg)

    pkg.write_bytes(b"a completely different replacement dump")
    os.utime(pkg, (pkg.stat().st_mtime + 5, pkg.stat().st_mtime + 5))

    assert shadps4._cache_key(pkg) != original_key


def test_run_pkg_extractor_raises_on_a_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Run pkg extractor raises on a nonzero exit."""
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: type("R", (), {"returncode": 2, "stderr": "boom"})(),
    )

    with pytest.raises(RuntimeError, match="exited 2"):
        shadps4._run_pkg_extractor(tmp_path / "Game.pkg", tmp_path / "dest")


def test_run_pkg_extractor_raises_when_the_binary_is_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Run pkg extractor raises when the binary is missing."""
    def raise_oserror(*a: object, **k: object) -> NoReturn:
        raise OSError("not found")

    monkeypatch.setattr(subprocess, "run", raise_oserror)

    with pytest.raises(RuntimeError, match="failed to run"):
        shadps4._run_pkg_extractor(tmp_path / "Game.pkg", tmp_path / "dest")


def test_extract_and_cache_pkg_reuses_an_existing_bootable_extraction(
    cache_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Extract and cache pkg reuses an existing bootable extraction."""
    pkg = tmp_path / "Game.pkg"
    pkg.write_bytes(b"pkg")
    key = shadps4._cache_key(pkg)
    eboot = _touch(cache_dir / key / "CUSA23079" / "eboot.bin")
    called = []
    monkeypatch.setattr(shadps4, "_run_pkg_extractor", lambda *a: called.append(a))

    boot = shadps4._extract_and_cache_pkg(pkg)

    assert boot == eboot
    assert called == []
    assert (cache_dir / key / shadps4._LAST_ACCESSED_MARKER).exists()


def test_extract_and_cache_pkg_re_extracts_a_stale_cache_dir_with_no_boot_target(
    cache_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Extract and cache pkg re-extracts a stale cache dir with no boot target."""
    pkg = tmp_path / "Game.pkg"
    pkg.write_bytes(b"pkg")
    stale = cache_dir / shadps4._cache_key(pkg)
    _touch(stale / "readme.txt")

    def fake_extract(pkg_: Path, dest: Path) -> None:
        _touch(dest / "CUSA23079" / "eboot.bin")

    monkeypatch.setattr(shadps4, "_run_pkg_extractor", fake_extract)

    boot = shadps4._extract_and_cache_pkg(pkg)

    assert boot.name == "eboot.bin"
    assert not (stale / "readme.txt").exists()


def test_extract_and_cache_pkg_extracts_and_returns_the_boot_target_on_a_miss(
    cache_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Extract and cache pkg extracts and returns the boot target on a miss."""
    pkg = tmp_path / "Game.pkg"
    pkg.write_bytes(b"pkg")

    def fake_extract(pkg_: Path, dest: Path) -> None:
        _touch(dest / "CUSA23079" / "eboot.bin")

    monkeypatch.setattr(shadps4, "_run_pkg_extractor", fake_extract)

    boot = shadps4._extract_and_cache_pkg(pkg)

    assert boot.name == "eboot.bin"
    assert (cache_dir / shadps4._cache_key(pkg) / shadps4._LAST_ACCESSED_MARKER).exists()


def test_extract_and_cache_pkg_cleans_up_and_raises_when_extraction_fails(
    cache_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Extract and cache pkg cleans up and raises when extraction fails."""
    pkg = tmp_path / "Game.pkg"
    pkg.write_bytes(b"pkg")

    def fake_extract(pkg_: Path, dest: Path) -> NoReturn:
        raise RuntimeError("boom")

    monkeypatch.setattr(shadps4, "_run_pkg_extractor", fake_extract)

    with pytest.raises(RuntimeError, match="boom"):
        shadps4._extract_and_cache_pkg(pkg)

    assert not (cache_dir / shadps4._cache_key(pkg)).exists()


def test_extract_and_cache_pkg_cleans_up_and_raises_when_nothing_bootable_was_extracted(
    cache_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Extract and cache pkg cleans up and raises when nothing bootable was extracted."""
    pkg = tmp_path / "Game.pkg"
    pkg.write_bytes(b"pkg")
    monkeypatch.setattr(shadps4, "_run_pkg_extractor", lambda p, d: None)

    with pytest.raises(RuntimeError, match="no eboot.bin"):
        shadps4._extract_and_cache_pkg(pkg)

    assert not (cache_dir / shadps4._cache_key(pkg)).exists()


def test_extract_and_cache_pkg_reserves_more_headroom_for_an_archive_than_a_pkg(
    cache_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An archive reserves headroom for both its scratch extraction and pkg_extractor's output."""
    seen_needed = []
    monkeypatch.setattr(shadps4, "_evict_lru", lambda needed, keep: seen_needed.append(needed))
    monkeypatch.setattr(shadps4, "_run_pkg_extractor", lambda p, d: _touch(d / "T" / "eboot.bin"))

    pkg = tmp_path / "Game.pkg"
    pkg.write_bytes(b"x" * 1000)
    shadps4._extract_and_cache_pkg(pkg)

    archive = _make_zip(tmp_path / "Game.zip", {"Game.pkg": b"x" * 1000})
    shadps4._extract_and_cache_pkg(archive)

    assert seen_needed == [
        int(pkg.stat().st_size * 1.1),
        int(archive.stat().st_size * 2.2),
    ]


def test_sweep_stale_extractions_removes_orphaned_scratch_dirs_but_keeps_real_ones(
    cache_dir: Path,
) -> None:
    """Sweep stale extractions removes leftover archive scratch dirs but keeps real cache entries."""
    scratch = _touch(cache_dir / "Game-abc123-archive-xyz987" / "leftover.pkg").parent
    game_dir = _touch(cache_dir / "Game-abc123" / "eboot.bin").parent

    shadps4.sweep_stale_extractions()

    assert not scratch.exists()
    assert game_dir.exists()


def test_sweep_stale_extractions_spares_a_cache_dir_whose_own_name_collides(
    cache_dir: Path,
) -> None:
    """A real, already-booted cache dir whose ROM stem itself contains '-archive-' survives the sweep.

    `_cache_key` names a cache dir `<rom stem>-<digest>`, so a ROM like
    "Uncharted-archive-Edition.pkg" produces a dir whose name matches the
    scratch-dir substring by coincidence. Only the last-accessed marker
    and boot target, both written solely to a real game_dir, tell them apart.
    """
    collider = cache_dir / "Uncharted-archive-Edition-a1b2c3d4e5f6"
    _touch(collider / "CUSA23079" / "eboot.bin")
    _touch(collider / shadps4._LAST_ACCESSED_MARKER)

    shadps4.sweep_stale_extractions()

    assert collider.exists()


def test_sweep_stale_extractions_spares_a_collider_with_a_boot_target_but_no_marker_yet(
    cache_dir: Path,
) -> None:
    """A colliding cache dir survives on a bootable eboot.bin alone, even before it is marked touched.

    Covers the process being killed between finding the boot target and
    writing the last-accessed marker in `_extract_and_cache_pkg`.
    """
    collider = cache_dir / "Uncharted-archive-Edition-a1b2c3d4e5f6"
    _touch(collider / "CUSA23079" / "eboot.bin")

    shadps4.sweep_stale_extractions()

    assert collider.exists()


def test_sweep_stale_extractions_is_a_noop_without_a_cache_dir(cache_dir: Path) -> None:
    """Sweep stale extractions is a no-op when the cache dir does not exist yet."""
    shadps4.sweep_stale_extractions()  # must not raise


def test_extract_and_cache_pkg_serializes_a_second_call_racing_the_same_pkg(
    cache_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A racing second call must wait for _CACHE_LOCK, not extract concurrently.

    A second launch racing in while the first is mid-extraction must wait
    for _CACHE_LOCK rather than run its own extraction concurrently, which
    could interleave writes into the same not-yet-populated game_dir or
    have one call evict the directory the other is about to boot from.
    """
    monkeypatch.setattr(shadps4, "CACHE_ENABLED", True)
    pkg = tmp_path / "Game.pkg"
    pkg.write_bytes(b"pkg")

    entered = threading.Event()
    release = threading.Event()
    entry_count: list[int] = []

    def fake_run_pkg_extractor(pkg_: Path, dest: Path) -> None:
        entry_count.append(1)
        entered.set()
        release.wait(timeout=5)
        (dest / "CUSA23079").mkdir(parents=True, exist_ok=True)
        (dest / "CUSA23079" / "eboot.bin").write_bytes(b"x")

    monkeypatch.setattr(shadps4, "_run_pkg_extractor", fake_run_pkg_extractor)

    first = threading.Thread(target=shadps4._extract_and_cache_pkg, args=(pkg,))
    first.start()
    assert entered.wait(timeout=5)

    second = threading.Thread(target=shadps4._extract_and_cache_pkg, args=(pkg,))
    second.start()
    time.sleep(0.2)
    # Still 1: the second call is blocked on _CACHE_LOCK, not free to run its
    # own extraction while the first is still inside the critical section.
    assert entry_count == [1]

    release.set()
    first.join(timeout=5)
    second.join(timeout=5)


# ── archive extraction (zip/7z/rar) feeding pkg_extractor ──────────────


def _make_zip(path: Path, members: dict) -> Path:
    """Write a real zip file at path with the given {member_name: bytes} contents."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return path


def test_archive_pkg_member_finds_a_pkg_inside_an_extracted_tree(tmp_path: Path) -> None:
    """Archive pkg member finds a .pkg file inside the extracted tree."""
    root = tmp_path / "extracted"
    pkg = _touch(root / "Game" / "CUSA23079.pkg")

    assert shadps4._archive_pkg_member(root) == pkg


def test_archive_pkg_member_is_none_without_a_pkg(tmp_path: Path) -> None:
    """Archive pkg member is none when the extracted tree holds no .pkg."""
    root = tmp_path / "extracted"
    _touch(root / "readme.txt")

    assert shadps4._archive_pkg_member(root) is None


def test_archive_pkg_member_rejects_a_pkg_that_symlinks_outside_root(tmp_path: Path) -> None:
    """Archive pkg member rejects a .pkg symlinked outside the extraction root."""
    outside = _touch(tmp_path / "outside" / "Game.pkg")
    root = tmp_path / "extracted"
    root.mkdir(parents=True)
    (root / "Game.pkg").symlink_to(outside)

    assert shadps4._archive_pkg_member(root) is None


def test_reject_unsafe_members_raises_on_a_zip_slip_path(tmp_path: Path) -> None:
    """Reject unsafe members raises on a member path that escapes dest."""
    dest = tmp_path / "dest"
    dest.mkdir()

    with pytest.raises(RuntimeError, match="escapes extraction dir"):
        shadps4._reject_unsafe_members(dest, ["../outside.txt"])


def test_reject_unsafe_members_allows_a_normal_relative_path(tmp_path: Path) -> None:
    """Reject unsafe members allows an ordinary relative member path."""
    dest = tmp_path / "dest"
    dest.mkdir()

    shadps4._reject_unsafe_members(dest, ["Game/CUSA23079.pkg"])  # must not raise


def test_extract_archive_unpacks_a_zip_via_the_stdlib(tmp_path: Path) -> None:
    """Extract archive unpacks a real .zip using the stdlib zipfile module."""
    archive = _make_zip(tmp_path / "Game.zip", {"CUSA23079.pkg": b"pkg data"})
    dest = tmp_path / "dest"
    dest.mkdir()

    shadps4._extract_archive(archive, dest)

    assert (dest / "CUSA23079.pkg").read_bytes() == b"pkg data"


def test_extract_archive_rejects_a_zip_slip_member(tmp_path: Path) -> None:
    """Extract archive raises rather than write a zip member outside dest."""
    archive = tmp_path / "Game.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../escaped.pkg", b"pkg data")
    dest = tmp_path / "dest"
    dest.mkdir()

    with pytest.raises(RuntimeError, match="escapes extraction dir"):
        shadps4._extract_archive(archive, dest)


def test_extract_archive_raises_on_a_corrupt_zip(tmp_path: Path) -> None:
    """Extract archive raises when the .zip is not a valid archive."""
    archive = tmp_path / "Game.zip"
    archive.write_bytes(b"not a zip")
    dest = tmp_path / "dest"
    dest.mkdir()

    with pytest.raises(RuntimeError, match="zip extraction"):
        shadps4._extract_archive(archive, dest)


def test_extract_archive_dispatches_7z_through_the_external_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Extract archive dispatches a .7z through the 7z listing and extraction tools."""
    archive = tmp_path / "Game.7z"
    archive.write_bytes(b"")
    dest = tmp_path / "dest"
    dest.mkdir()
    calls = []
    monkeypatch.setattr(shadps4, "_7z_member_paths", lambda a: ["CUSA23079.pkg"])

    def fake_run_extractor(cmd: list, what: str) -> str:
        calls.append(cmd)
        (dest / "CUSA23079.pkg").write_bytes(b"pkg data")
        return ""

    monkeypatch.setattr(shadps4, "_run_extractor", fake_run_extractor)

    shadps4._extract_archive(archive, dest)

    assert calls and calls[0][0] == "7z"
    assert (dest / "CUSA23079.pkg").exists()


def test_extract_archive_dispatches_rar_through_the_external_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Extract archive dispatches a .rar through the unrar listing and extraction tools."""
    archive = tmp_path / "Game.rar"
    archive.write_bytes(b"")
    dest = tmp_path / "dest"
    dest.mkdir()
    calls = []
    monkeypatch.setattr(shadps4, "_rar_member_paths", lambda a: ["CUSA23079.pkg"])

    def fake_run_extractor(cmd: list, what: str) -> str:
        calls.append(cmd)
        (dest / "CUSA23079.pkg").write_bytes(b"pkg data")
        return ""

    monkeypatch.setattr(shadps4, "_run_extractor", fake_run_extractor)

    shadps4._extract_archive(archive, dest)

    assert calls and calls[0][0] == "unrar"
    assert (dest / "CUSA23079.pkg").exists()


def test_reject_escaped_tree_raises_when_a_symlink_escapes_dest(tmp_path: Path) -> None:
    """Reject escaped tree raises when a real symlink resolves outside dest."""
    outside = _touch(tmp_path / "outside.pkg")
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "escaped.pkg").symlink_to(outside)

    with pytest.raises(RuntimeError, match="escapes cache dir"):
        shadps4._reject_escaped_tree(dest)


def test_reject_escaped_tree_allows_a_normal_extraction(tmp_path: Path) -> None:
    """Reject escaped tree allows a normal, fully-contained extraction."""
    dest = tmp_path / "dest"
    _touch(dest / "Game" / "CUSA23079.pkg")

    shadps4._reject_escaped_tree(dest)  # must not raise


def test_run_extractor_raises_on_a_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run extractor raises on a nonzero exit."""
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: type("R", (), {"returncode": 2, "stderr": "boom", "stdout": ""})(),
    )

    with pytest.raises(RuntimeError, match="exited 2"):
        shadps4._run_extractor(["7z", "l"], "7z list (Game.7z)")


def test_run_extractor_raises_when_the_binary_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run extractor raises when the underlying binary cannot be run."""
    def raise_oserror(*a: object, **k: object) -> NoReturn:
        raise OSError("not found")

    monkeypatch.setattr(subprocess, "run", raise_oserror)

    with pytest.raises(RuntimeError, match="failed to run"):
        shadps4._run_extractor(["unrar", "lb"], "unrar list (Game.rar)")


def test_extract_and_cache_pkg_extracts_an_archive_and_returns_the_boot_target(
    cache_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Extract and cache pkg unpacks an archive, extracts the .pkg it holds, and boots the result."""
    archive = _make_zip(tmp_path / "Game.zip", {"CUSA23079.pkg": b"pkg data"})
    seen_pkgs = []

    def fake_run_pkg_extractor(pkg: Path, dest: Path) -> None:
        seen_pkgs.append(pkg.name)
        _touch(dest / "CUSA23079" / "eboot.bin")

    monkeypatch.setattr(shadps4, "_run_pkg_extractor", fake_run_pkg_extractor)

    boot = shadps4._extract_and_cache_pkg(archive)

    assert boot.name == "eboot.bin"
    assert seen_pkgs == ["CUSA23079.pkg"]
    # Only pkg_extractor's own output survives; the scratch archive
    # extraction (a sibling of game_dir under cache_dir) is discarded.
    remaining_top_level = {p.name for p in cache_dir.iterdir()}
    assert remaining_top_level == {shadps4._cache_key(archive)}


def test_extract_and_cache_pkg_reuses_a_cached_archive_extraction(
    cache_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Extract and cache pkg reuses a cached extraction on a relaunch of the same archive."""
    archive = _make_zip(tmp_path / "Game.zip", {"CUSA23079.pkg": b"pkg data"})
    key = shadps4._cache_key(archive)
    eboot = _touch(cache_dir / key / "CUSA23079" / "eboot.bin")
    called = []
    monkeypatch.setattr(shadps4, "_extract_archive", lambda *a: called.append(a))

    boot = shadps4._extract_and_cache_pkg(archive)

    assert boot == eboot
    assert called == []


def test_extract_and_cache_pkg_raises_when_the_archive_holds_no_pkg(
    cache_dir: Path, tmp_path: Path
) -> None:
    """Extract and cache pkg cleans up and raises when the archive holds no .pkg."""
    archive = _make_zip(tmp_path / "Game.zip", {"readme.txt": b"nothing bootable"})

    with pytest.raises(RuntimeError, match=r"held no \.pkg"):
        shadps4._extract_and_cache_pkg(archive)

    assert not (cache_dir / shadps4._cache_key(archive)).exists()


def test_launch_extracts_and_boots_from_a_zip_archive(
    monkeypatch: pytest.MonkeyPatch, versions_dir: Path, rom_root: Path, tmp_path: Path
) -> None:
    """Launch dispatches a .zip ROM through the same extraction cache as a .pkg."""
    binary = _make_release(versions_dir, "v0.17.0 - Only Release")
    monkeypatch.setattr(shadps4.Shadps4, "stop", lambda self: None)
    spawned = {}

    def fake_spawn(
        self: shadps4.Shadps4, cmd: list[str], env: dict[str, str], stdin_pipe: bool = False
    ) -> None:
        spawned["cmd"] = cmd
        self._proc = _FakeProc()

    monkeypatch.setattr(shadps4.Shadps4, "_spawn", fake_spawn)
    extracted_boot = _touch(tmp_path / "extracted" / "CUSA23079" / "eboot.bin")
    monkeypatch.setattr(shadps4, "_extract_and_cache_pkg", lambda rom: extracted_boot)
    rom = rom_root / "game.zip"
    rom.write_bytes(b"")
    emu = shadps4.Shadps4()

    emu.launch(rom, resume_slot=None)

    assert spawned["cmd"] == [str(binary), "-f", "true", "-g", str(extracted_boot)]
    assert emu._extracted_dir == shadps4.CACHE_DIR / shadps4._cache_key(rom)


def test_launch_extracts_and_boots_from_a_pkg_rom(
    monkeypatch: pytest.MonkeyPatch, versions_dir: Path, rom_root: Path, tmp_path: Path
) -> None:
    """Launch extracts a .pkg ROM through the cache and boots the extracted eboot.bin."""
    binary = _make_release(versions_dir, "v0.17.0 - Only Release")
    monkeypatch.setattr(shadps4.Shadps4, "stop", lambda self: None)
    spawned = {}

    def fake_spawn(
        self: shadps4.Shadps4, cmd: list[str], env: dict[str, str], stdin_pipe: bool = False
    ) -> None:
        spawned["cmd"] = cmd
        self._proc = _FakeProc()

    monkeypatch.setattr(shadps4.Shadps4, "_spawn", fake_spawn)
    extracted_boot = _touch(tmp_path / "extracted" / "CUSA23079" / "eboot.bin")
    monkeypatch.setattr(shadps4, "_extract_and_cache_pkg", lambda pkg: extracted_boot)
    rom = rom_root / "game.pkg"
    rom.write_bytes(b"")
    emu = shadps4.Shadps4()

    emu.launch(rom, resume_slot=None)

    assert spawned["cmd"] == [str(binary), "-f", "true", "-g", str(extracted_boot)]
    assert emu._extracted_dir == shadps4.CACHE_DIR / shadps4._cache_key(rom)


def test_stop_removes_the_pkg_extraction_when_the_cache_is_disabled(
    monkeypatch: pytest.MonkeyPatch, cache_dir: Path, versions_dir: Path, rom_root: Path
) -> None:
    """Stop removes the pkg extraction when the cache is disabled."""
    _make_release(versions_dir, "v0.17.0 - Only Release")
    monkeypatch.setattr(shadps4.Shadps4, "_spawn", lambda self, cmd, env, stdin_pipe=False: None)
    rom = rom_root / "game.pkg"
    rom.write_bytes(b"")
    game_dir = cache_dir / shadps4._cache_key(rom)
    monkeypatch.setattr(shadps4, "_extract_and_cache_pkg", lambda pkg: _touch(game_dir / "eboot.bin"))

    emu = shadps4.Shadps4()
    emu.launch(rom, resume_slot=None)
    assert game_dir.exists()

    emu.stop()

    assert not game_dir.exists()


def test_stop_keeps_the_pkg_extraction_when_the_cache_is_enabled(
    monkeypatch: pytest.MonkeyPatch, cache_dir: Path, versions_dir: Path, rom_root: Path
) -> None:
    """Stop keeps the pkg extraction when the cache is enabled."""
    monkeypatch.setattr(shadps4, "CACHE_ENABLED", True)
    _make_release(versions_dir, "v0.17.0 - Only Release")
    monkeypatch.setattr(shadps4.Shadps4, "_spawn", lambda self, cmd, env, stdin_pipe=False: None)
    rom = rom_root / "game.pkg"
    rom.write_bytes(b"")
    game_dir = cache_dir / shadps4._cache_key(rom)
    monkeypatch.setattr(shadps4, "_extract_and_cache_pkg", lambda pkg: _touch(game_dir / "eboot.bin"))

    emu = shadps4.Shadps4()
    emu.launch(rom, resume_slot=None)

    emu.stop()

    assert game_dir.exists()


def test_the_cache_is_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cache is disabled by default when SHADPS4_CACHE_ENABLED is unset."""
    monkeypatch.delenv("SHADPS4_CACHE_ENABLED", raising=False)
    assert shadps4._truthy(os.environ.get("SHADPS4_CACHE_ENABLED", "false")) is False
