# shadPS4 Parity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the symlink-escape gap in shadPS4's `resolve_rom_file` and bring its test coverage up to parity with the other emulator modules (PCSX2, Dolphin).

**Architecture:** One production fix confined to `webstation_broker/emulators/shadps4.py`'s `resolve_rom_file` method, plus a new `tests/test_shadps4.py` covering the module's four public surfaces (`_resolve_binary`, `resolve_rom_file`, `launch`, `stop`) with mocked subprocess/IPC, following the fixture and monkeypatching conventions already used by `tests/test_pcsx2.py` and `tests/test_dolphin.py`.

**Tech Stack:** Python stdlib, pytest, `pytest.monkeypatch`, `tmp_path`. No new dependencies.

## Global Constraints

- No boot-failure watchdog for shadPS4 (no status-query oracle exists for its stdin-only IPC) — out of scope per the approved spec.
- No live/container end-to-end verification in this pass — code + mocked tests only.
- No changes to save-data handling, IPC protocol, or version-resolution semantics beyond the one symlink-validation fix.
- Follow this repo's existing local convention: emulator modules read config via inline `os.environ.get(...)` at import time into module globals; tests redirect those globals with `monkeypatch.setattr(module, "GLOBAL", ...)`, never the env var itself (paths are already baked into the global by the time a test runs).
- No new third-party dependencies.

---

### Task 1: Fix the `resolve_rom_file` symlink escape + its tests

**Files:**
- Modify: `webstation_broker/emulators/shadps4.py:110-118` (the `resolve_rom_file` method)
- Create: `tests/test_shadps4.py` (new file; this task adds its header, imports, `rom_root` fixture, and all `resolve_rom_file` tests)

**Interfaces:**
- Consumes: `webstation_broker.emulators.shadps4.Shadps4` (existing class), `webstation_broker.emulators.shadps4.ROM_ROOT` (existing module global, a `Path`)
- Produces: `rom_root` fixture (in `tests/test_shadps4.py`) — creates `tmp_path / "romm"`, monkeypatches `shadps4.ROM_ROOT` to it, returns the `Path`. Later tasks in this file do not depend on it, but it establishes the file's fixture-naming convention (`versions_dir` in Task 2 follows the same shape).

- [ ] **Step 1: Create the test file with its header, imports, and the `rom_root` fixture, plus the symlink-escape regression test**

```python
"""shadPS4 ROM resolution, binary version selection, launch, and IPC-driven stop."""

import subprocess
from pathlib import Path

import pytest

from webstation_broker.emulators import base, shadps4


@pytest.fixture
def rom_root(monkeypatch, tmp_path):
    root = tmp_path / "romm"
    root.mkdir()
    monkeypatch.setattr(shadps4, "ROM_ROOT", root)
    return root


def test_resolve_refuses_an_eboot_that_symlinks_out_of_the_rom_root(rom_root, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.bin"
    secret.write_bytes(b"not a game")
    folder = rom_root / "MyGame"
    folder.mkdir()
    (folder / "eboot.bin").symlink_to(secret)

    assert shadps4.Shadps4().resolve_rom_file(folder) is None
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_shadps4.py::test_resolve_refuses_an_eboot_that_symlinks_out_of_the_rom_root -v`
Expected: FAIL — the current implementation follows the symlink and returns the `eboot.bin` path instead of `None`.

- [ ] **Step 3: Apply the fix**

In `webstation_broker/emulators/shadps4.py`, replace:

```python
    def resolve_rom_file(self, path: Path) -> Path | None:
        if path.is_file():
            return path
        if not path.is_dir():
            return None
        eboot = path / "eboot.bin"
        if eboot.is_file():
            return eboot
        return path  # shadps4 appends eboot.bin to directory paths itself
```

with:

```python
    def resolve_rom_file(self, path: Path) -> Path | None:
        if path.is_file():
            return path
        if not path.is_dir():
            return None
        eboot = path / "eboot.bin"
        try:
            if eboot.is_file() and eboot.resolve().is_relative_to(ROM_ROOT):
                return eboot
        except OSError:
            return None
        return path  # shadps4 appends eboot.bin to directory paths itself
```

- [ ] **Step 4: Run it to verify it passes**

Run: `uv run pytest tests/test_shadps4.py::test_resolve_refuses_an_eboot_that_symlinks_out_of_the_rom_root -v`
Expected: PASS

- [ ] **Step 5: Add the remaining `resolve_rom_file` characterization tests**

Append to `tests/test_shadps4.py`:

```python
def test_resolve_takes_a_direct_file_as_given(rom_root):
    rom = rom_root / "game.zar"
    rom.write_bytes(b"")

    assert shadps4.Shadps4().resolve_rom_file(rom) == rom


def test_resolve_finds_eboot_inside_a_game_folder(rom_root):
    folder = rom_root / "MyGame"
    folder.mkdir()
    eboot = folder / "eboot.bin"
    eboot.write_bytes(b"")

    assert shadps4.Shadps4().resolve_rom_file(folder) == eboot


def test_resolve_falls_back_to_the_bare_folder_when_there_is_no_eboot(rom_root):
    folder = rom_root / "MyGame"
    folder.mkdir()

    assert shadps4.Shadps4().resolve_rom_file(folder) == folder


def test_resolve_returns_nothing_for_a_path_that_is_neither_file_nor_folder(rom_root):
    missing = rom_root / "nope"

    assert shadps4.Shadps4().resolve_rom_file(missing) is None
```

These exercise behavior the fix does not change, so no red phase is expected — they should pass immediately.

- [ ] **Step 6: Run the whole file to verify all five pass**

Run: `uv run pytest tests/test_shadps4.py -v`
Expected: 5 passed

- [ ] **Step 7: Commit**

```bash
git add webstation_broker/emulators/shadps4.py tests/test_shadps4.py
git commit -m "fix: validate eboot.bin stays inside ROM_ROOT in shadps4 resolve_rom_file"
```

---

### Task 2: `_resolve_binary()` test coverage

**Files:**
- Test: `tests/test_shadps4.py` (append to the file from Task 1)

**Interfaces:**
- Consumes: `webstation_broker.emulators.shadps4._resolve_binary()` (existing module function, no args, returns `Path | None`), `webstation_broker.emulators.shadps4.VERSIONS_DIR` (existing module global, a `Path`), `SHADPS4_BIN` env var (read directly inside `_resolve_binary()` via `os.environ.get`, no override needed — validated unconditionally, not checked for existence), `webstation_broker.emulators.shadps4.BIN_NAME` (existing module global, default `"Shadps4-sdl.AppImage"`)
- Produces: `versions_dir` fixture and `_make_release(base, folder_name, bin_name=...)` helper (in `tests/test_shadps4.py`) — Task 3 reuses both.

- [ ] **Step 1: Write the fixture, helper, and all `_resolve_binary` tests**

Append to `tests/test_shadps4.py`:

```python
@pytest.fixture
def versions_dir(monkeypatch, tmp_path):
    d = tmp_path / "versions"
    d.mkdir()
    monkeypatch.setattr(shadps4, "VERSIONS_DIR", d)
    monkeypatch.delenv("SHADPS4_BIN", raising=False)
    return d


def _make_release(base: Path, folder_name: str, bin_name: str = "Shadps4-sdl.AppImage") -> Path:
    folder = base / folder_name
    folder.mkdir(parents=True)
    binary = folder / bin_name
    binary.write_bytes(b"")
    return binary


def test_an_explicit_override_wins_over_everything(versions_dir, monkeypatch):
    _make_release(versions_dir, "v0.17.0 - Garbage Collector's Edition")
    monkeypatch.setenv("SHADPS4_BIN", "/opt/custom/shadps4")

    assert shadps4._resolve_binary() == Path("/opt/custom/shadps4")


def test_the_pre_release_folder_beats_every_numbered_release(versions_dir):
    _make_release(versions_dir, "v99.0.0 - Newest Looking Number")
    pre = _make_release(versions_dir, "Pre-release")

    assert shadps4._resolve_binary() == pre


def test_the_highest_semver_release_wins_by_number_not_by_string(versions_dir):
    _make_release(versions_dir, "v0.9.9 - A")
    newest = _make_release(versions_dir, "v0.9.10 - B")

    assert shadps4._resolve_binary() == newest


def test_release_folder_precedence_across_more_than_two_versions(versions_dir):
    _make_release(versions_dir, "v0.9.9 - A")
    _make_release(versions_dir, "v0.9.10 - B")
    newest = _make_release(
        versions_dir, "v0.17.0 - Garbage Collector's Edition - 2026-07-30"
    )

    assert shadps4._resolve_binary() == newest


def test_a_folder_whose_name_does_not_parse_as_a_version_is_skipped_not_fatal(versions_dir):
    (versions_dir / "notes").mkdir()
    (versions_dir / "notes" / "Shadps4-sdl.AppImage").write_bytes(b"")
    newest = _make_release(versions_dir, "v0.5.0 - Only Real Release")

    assert shadps4._resolve_binary() == newest


def test_a_missing_versions_dir_resolves_to_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(shadps4, "VERSIONS_DIR", tmp_path / "does-not-exist")
    monkeypatch.delenv("SHADPS4_BIN", raising=False)

    assert shadps4._resolve_binary() is None


def test_a_versions_dir_with_no_usable_binary_resolves_to_nothing(versions_dir):
    (versions_dir / "v0.1.0 - Empty").mkdir()

    assert shadps4._resolve_binary() is None
```

- [ ] **Step 2: Run the new tests**

Run: `uv run pytest tests/test_shadps4.py -v -k resolve_binary or override or pre_release or semver or precedence or does_not_parse or missing_versions or no_usable_binary`
Expected: All new tests PASS immediately — `_resolve_binary()` is existing, already-correct behavior being characterized, not changed.

If any fail, read the failure against the actual `_resolve_binary()`/`_find_binary_in()` implementation in `webstation_broker/emulators/shadps4.py` before changing the test — a failure here means the test's assumption about existing behavior is wrong, not that production code needs to change (Task 2 makes no production changes).

- [ ] **Step 3: Commit**

```bash
git add tests/test_shadps4.py
git commit -m "test: cover shadps4 binary version resolution"
```

---

### Task 3: `launch()` test coverage

**Files:**
- Test: `tests/test_shadps4.py` (append to the file from Tasks 1-2)

**Interfaces:**
- Consumes: `rom_root` fixture (Task 1), `versions_dir` fixture + `_make_release` helper (Task 2), `webstation_broker.emulators.shadps4.Shadps4.launch(self, rom_path: Path, resume_slot: int | None) -> None` (existing method), `webstation_broker.emulators.shadps4.Shadps4._spawn(self, cmd: list[str], env: dict[str, str], stdin_pipe: bool = False) -> None` (existing method, inherited from `Emulator`, monkeypatched per-test to avoid a real subprocess)
- Produces: `_FakeStdin` and `_FakeProc` helper classes (in `tests/test_shadps4.py`) — Task 4 reuses both.

- [ ] **Step 1: Write the fake process helpers and the launch tests**

Append to `tests/test_shadps4.py`:

```python
class _FakeStdin:
    def __init__(self, fail: bool = False):
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
    def __init__(self, pid: int = 4242):
        self.pid = pid
        self.stdin = _FakeStdin()
        self.wait_calls: list[float | None] = []
        self.wait_exc: Exception | None = None

    def poll(self):
        return None

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        if self.wait_exc is not None:
            raise self.wait_exc
        return 0


def test_launch_stops_first_then_spawns_with_ipc_enabled(monkeypatch, versions_dir, rom_root):
    binary = _make_release(versions_dir, "v0.17.0 - Only Release")
    stopped = []
    monkeypatch.setattr(shadps4.Shadps4, "stop", lambda self: stopped.append(True))
    spawned = {}

    def fake_spawn(self, cmd, env, stdin_pipe=False):
        spawned["cmd"] = cmd
        spawned["env"] = env
        spawned["stdin_pipe"] = stdin_pipe
        self._proc = _FakeProc()

    monkeypatch.setattr(shadps4.Shadps4, "_spawn", fake_spawn)
    rom = rom_root / "game.zar"
    rom.write_bytes(b"")
    emu = shadps4.Shadps4()

    emu.launch(rom, resume_slot=None)

    assert stopped == [True]
    assert spawned["cmd"] == [str(binary), "-f", "true", "-g", str(rom)]
    assert spawned["env"]["SHADPS4_ENABLE_IPC"] == "true"
    assert spawned["stdin_pipe"] is True
    assert emu._proc.stdin.written == [b"RUN\n", b"START\n"]


def test_launch_logs_and_ignores_a_resume_slot(monkeypatch, versions_dir, rom_root, caplog):
    _make_release(versions_dir, "v0.17.0 - Only Release")
    monkeypatch.setattr(shadps4.Shadps4, "stop", lambda self: None)

    def fake_spawn(self, cmd, env, stdin_pipe=False):
        self._proc = _FakeProc()

    monkeypatch.setattr(shadps4.Shadps4, "_spawn", fake_spawn)
    rom = rom_root / "game.zar"
    rom.write_bytes(b"")
    emu = shadps4.Shadps4()

    with caplog.at_level("INFO"):
        emu.launch(rom, resume_slot=3)

    assert "resume_slot" in caplog.text
    assert emu._proc.stdin.written == [b"RUN\n", b"START\n"]


def test_launch_raises_when_no_binary_is_available(monkeypatch, tmp_path, rom_root):
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
```

- [ ] **Step 2: Run the new tests**

Run: `uv run pytest tests/test_shadps4.py -v -k launch`
Expected: All PASS immediately — `launch()` is existing, already-correct behavior being characterized.

If `test_launch_stops_first_then_spawns_with_ipc_enabled` fails on the `cmd` assertion, read the actual argument list built in `Shadps4.launch` in `webstation_broker/emulators/shadps4.py` and correct the expected list in the test to match — do not change production code in this task.

- [ ] **Step 3: Commit**

```bash
git add tests/test_shadps4.py
git commit -m "test: cover shadps4 launch behavior"
```

---

### Task 4: `stop()` test coverage

**Files:**
- Test: `tests/test_shadps4.py` (append to the file from Tasks 1-3)

**Interfaces:**
- Consumes: `_FakeProc`/`_FakeStdin` helpers (Task 3), `webstation_broker.emulators.shadps4.Shadps4.stop(self) -> None` (existing method), `webstation_broker.emulators.base.Emulator.stop` (existing method, monkeypatched as a spy to detect the SIGTERM-escalation fallback without sending a real signal to a nonexistent pid)
- Produces: nothing consumed by later tasks (this is the last test task).

- [ ] **Step 1: Write the stop tests**

Append to `tests/test_shadps4.py`:

```python
def test_stop_sends_ipc_stop_and_waits_for_a_graceful_exit(monkeypatch):
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


def test_stop_falls_back_to_sigterm_escalation_when_the_stdin_write_fails(monkeypatch):
    escalated = []
    monkeypatch.setattr(base.Emulator, "stop", lambda self: escalated.append(True))
    emu = shadps4.Shadps4()
    proc = _FakeProc()
    proc.stdin.fail = True
    emu._proc = proc

    emu.stop()

    assert escalated == [True]


def test_stop_falls_back_to_sigterm_escalation_when_the_process_never_exits(monkeypatch):
    escalated = []
    monkeypatch.setattr(base.Emulator, "stop", lambda self: escalated.append(True))
    emu = shadps4.Shadps4()
    proc = _FakeProc()
    proc.wait_exc = subprocess.TimeoutExpired(cmd="shadps4", timeout=emu.term_timeout)
    emu._proc = proc

    emu.stop()

    assert proc.stdin.written == [b"STOP\n"]
    assert escalated == [True]


def test_stop_is_a_no_op_when_nothing_is_running():
    emu = shadps4.Shadps4()
    emu._proc = None

    emu.stop()

    assert emu._proc is None
```

- [ ] **Step 2: Run the new tests**

Run: `uv run pytest tests/test_shadps4.py -v -k stop`
Expected: All PASS immediately — `stop()` is existing, already-correct behavior being characterized.

- [ ] **Step 3: Commit**

```bash
git add tests/test_shadps4.py
git commit -m "test: cover shadps4 stop and sigterm escalation fallback"
```

---

### Task 5: Full regression run

**Files:** none (verification only)

**Interfaces:** none

- [ ] **Step 1: Run the new file in isolation**

Run: `uv run pytest tests/test_shadps4.py -v`
Expected: all tests pass (20 tests: 5 from Task 1, 7 from Task 2, 3 from Task 3, 4 from Task 4).

- [ ] **Step 2: Run the full suite**

Run: `uv run pytest`
Expected: no failures anywhere else. The `resolve_rom_file` change only touches `webstation_broker/emulators/shadps4.py`, so `tests/test_emulators.py`'s shadps4-related case and `tests/test_saves.py`'s shadps4 corrupted-marker test should be unaffected, but this step confirms it.

- [ ] **Step 3: If ruff is configured for this repo, lint the new/changed files**

Run: `uv run ruff check webstation_broker/emulators/shadps4.py tests/test_shadps4.py`
Expected: clean. Fix any findings before proceeding.

- [ ] **Step 4: Final commit if Step 3 produced fixes**

```bash
git add webstation_broker/emulators/shadps4.py tests/test_shadps4.py
git commit -m "style: lint fixes for shadps4 parity pass"
```

(Skip this step entirely if Step 3 found nothing to fix.)

---

## Self-Review

**Spec coverage:**
- Change 1 (symlink-escape fix) → Task 1, Steps 1-4. ✓
- Change 2, `_resolve_binary()` coverage (override precedence, Pre-release precedence, semver numeric-not-lexicographic, unparseable-folder skip, missing VERSIONS_DIR, no-usable-binary) → Task 2, all seven tests map 1:1 to the spec's bullet list. ✓
- Change 2, `resolve_rom_file()` coverage (direct file, folder w/ eboot, folder w/o eboot, symlink-escape regression, non-file/non-dir) → Task 1, five tests. ✓
- Change 2, `launch()` coverage (stop-first, spawn args/env/stdin_pipe, RUN-then-START order, resume_slot logged+ignored, RuntimeError on no binary) → Task 3, three tests (the stop-first, spawn-args, and RUN/START-order assertions are combined into one test since they're all observable from a single `launch()` call, per the spec's own grouping). ✓
- Change 2, `stop()` coverage (graceful path, BrokenPipeError/OSError fallback, TimeoutExpired fallback, no-op) → Task 4, four tests. ✓
- Testing section (`pytest tests/test_shadps4.py -v` + full suite) → Task 5. ✓
- Non-goals (no boot watchdog, no live verification, no protocol/version-semantics changes) → stated in Global Constraints, no task introduces them. ✓

**Placeholder scan:** no TBD/TODO markers; every step has literal, runnable code; no "similar to Task N" references — Task 4's tests are fully written out despite reusing Task 3's helper classes.

**Type consistency:** `_FakeProc`/`_FakeStdin` signatures introduced in Task 3 are used unchanged in Task 4 (`proc.stdin.written`, `proc.stdin.flush_count`, `proc.wait_calls`, `proc.stdin.fail`, `proc.wait_exc`). `_make_release(base: Path, folder_name: str, bin_name: str = "Shadps4-sdl.AppImage") -> Path` from Task 2 is called with matching positional args in Task 3. `rom_root` fixture from Task 1 returns a `Path` and is consumed identically in Tasks 3. The fix in Task 1 references `ROM_ROOT`, which is already imported at module scope in `shadps4.py` (confirmed by reading the file) — no new import needed.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-08-20-shadps4-parity.md`. Two execution options:

1. **Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration
2. **Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints

Which approach?
