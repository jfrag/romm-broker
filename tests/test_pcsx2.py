"""PCSX2 state naming and slot resolution.

Also covers the memory card contract and the boot watchdog that flags a VM
that never comes up.
"""

import os
from pathlib import Path
from typing import Any

import pytest

from webstation_broker.emulators import pcsx2


@pytest.fixture
def sstate_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the PCSX2 state directory at a fresh directory under tmp_path.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
        tmp_path: The per-test temporary directory.

    Returns:
        The state directory.
    """
    d = tmp_path / "sstates"
    d.mkdir()
    monkeypatch.setattr(pcsx2, "SSTATE_DIR", d)
    return d


def _touch(path: Path, mtime: float | None = None) -> Path:
    """Write a placeholder state file, optionally with a fixed mtime.

    Args:
        path: The file to create.
        mtime: Modification time to stamp on it, if any.

    Returns:
        The path that was written.
    """
    path.write_bytes(b"state")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("SLUS-20946 (7D3A8B4E).01.p2s", "SLUS-20946 (7D3A8B4E).10.p2s"),
        # A capture from a single-digit slot still lands in the working slot.
        ("SLUS-20946 (7D3A8B4E).9.p2s", "SLUS-20946 (7D3A8B4E).10.p2s"),
        ("SLUS-20946 (7D3A8B4E).10.p2s", "SLUS-20946 (7D3A8B4E).10.p2s"),
    ],
)
def test_restamp_keeps_the_serial_and_rewrites_the_slot(filename: str, expected: str) -> None:
    """Restamping keeps the serial and CRC and rewrites only the slot number."""
    assert pcsx2._restamp_slot(filename, 10) == expected


@pytest.mark.parametrize("filename", ["SLUS-20946.p2s", "SLUS-20946.10.sav", "card.bin", ""])
def test_restamp_refuses_anything_that_is_not_a_state_name(filename: str) -> None:
    """Restamping returns None for a name that is not a PCSX2 state name."""
    assert pcsx2._restamp_slot(filename, 10) is None


@pytest.mark.parametrize(
    ("name", "slot", "expected"),
    [
        ("SLUS-20946.10.p2s", 10, True),
        ("SLUS-20946.01.p2s", 1, True),
        ("SLUS-20946.1.p2s", 1, True),
        ("SLUS-20946.02.p2s", 1, False),
    ],
)
def test_slot_match_accepts_both_widths_pcsx2_writes(name: str, slot: int, expected: bool) -> None:
    """A slot matches whether PCSX2 wrote it with one digit or two."""
    assert pcsx2._matches_slot(Path(name), slot) is expected


def test_working_slot_reads_the_newest_state_in_it(sstate_dir: Path) -> None:
    """The working slot resolves to the newest state in that slot, ignoring other slots."""
    _touch(sstate_dir / "SLUS-1.10.p2s", mtime=1000)
    newest = _touch(sstate_dir / "SLUS-2.10.p2s", mtime=3000)
    _touch(sstate_dir / "SLUS-3.02.p2s", mtime=9000)

    assert pcsx2.newest_state_for_slot(10) == newest


def test_working_slot_is_empty_when_it_holds_nothing(sstate_dir: Path) -> None:
    """The working slot resolves to None when only other slots hold states."""
    _touch(sstate_dir / "SLUS-3.02.p2s")

    assert pcsx2.newest_state_for_slot(10) is None


def test_state_target_names_a_push_for_the_working_slot(
    sstate_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pushed state is targeted at the working slot under its own serial."""
    monkeypatch.setattr(pcsx2, "STATE_SLOT", 10)

    target = pcsx2.Pcsx2().state_target("SLUS-20946 (7D3A8B4E).03.p2s")

    assert target == sstate_dir / "SLUS-20946 (7D3A8B4E).10.p2s"


def test_state_target_refuses_another_disc_over_the_state_in_the_slot(
    sstate_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A push for another disc cannot land on top of the state already in the slot."""
    monkeypatch.setattr(pcsx2, "STATE_SLOT", 10)
    existing = _touch(sstate_dir / "SLUS-20946 (7D3A8B4E).10.p2s")

    assert pcsx2.Pcsx2().state_target("SLUS-20946 (7D3A8B4E).01.p2s") == existing
    assert pcsx2.Pcsx2().state_target("SLES-51234 (00000000).01.p2s") is None


@pytest.mark.parametrize("filename", ["../escape.01.p2s", "", ".", "..", "card.bin"])
def test_state_target_refuses_a_name_pcsx2_would_never_write(sstate_dir: Path, filename: str) -> None:
    """A push whose name PCSX2 would never write is refused."""
    assert pcsx2.Pcsx2().state_target(filename) is None


def test_clearing_the_slot_leaves_the_other_slots_alone(
    sstate_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Clearing the working slot removes its state and keeps the other slots."""
    monkeypatch.setattr(pcsx2, "STATE_SLOT", 10)
    stale = _touch(sstate_dir / "SLUS-20946.10.p2s")
    other = _touch(sstate_dir / "SLUS-20946.02.p2s")

    pcsx2.Pcsx2().clear_working_slot()

    assert not stale.exists()
    assert other.exists()


def test_the_card_the_whole_card_routes_sync_is_the_slot_1_folder() -> None:
    """The memory card the routes sync is the slot 1 folder card inside the save archive."""
    emu = pcsx2.Pcsx2()

    assert emu.memory_card_path().parent == pcsx2.MEMCARD_DIR
    # The card rides the memory-card routes, so activate has to be able to take
    # it back out of the save archive by name.
    assert emu.memory_card_subtree in emu.save_subtrees
    assert emu.memory_card_marker


class _FakeClock:
    """A monotonic() stand-in that advances by `step` seconds each call.

    A 90s deadline resolves in microseconds of real test time.

    Attributes:
        now: The time the last call returned.
        step: Seconds added on every call.
    """

    def __init__(self, step: float = 30.0) -> None:
        """Start the clock at zero.

        Args:
            step: Seconds the clock advances on every call.
        """
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        """Advance the clock and return the new time.

        Returns:
            The current fake monotonic time.
        """
        self.now += self.step
        return self.now


@pytest.fixture
def watchdog_env(monkeypatch: pytest.MonkeyPatch) -> _FakeClock:
    """Patch sleeping and the clock so _boot_watchdog tests finish instantly.

    No real sleeping, no real PINE socket, a clock that reaches the 90s
    deadline in a handful of calls.

    Args:
        monkeypatch: The pytest monkeypatch fixture.

    Returns:
        The fake clock installed as time.monotonic.
    """
    monkeypatch.setattr(pcsx2.time, "sleep", lambda _seconds: None)
    clock = _FakeClock()
    monkeypatch.setattr(pcsx2.time, "monotonic", clock)
    return clock


def test_boot_watchdog_clears_the_flag_when_the_vm_boots_promptly(
    monkeypatch: pytest.MonkeyPatch, watchdog_env: _FakeClock
) -> None:
    """The watchdog leaves boot_failed clear when PINE reports the VM running."""
    monkeypatch.setattr(pcsx2, "_pine_emu_status", lambda: 0)
    monkeypatch.setattr(pcsx2.Pcsx2, "wait_for_state", lambda self, deadline: True)
    monkeypatch.setattr(pcsx2.Pcsx2, "load_state", lambda self, slot: True)
    emu = pcsx2.Pcsx2()

    emu._boot_watchdog(1, emu._launch_seq)

    assert emu.boot_failed is False


def test_boot_watchdog_flags_a_hang_when_the_process_is_still_alive(
    monkeypatch: pytest.MonkeyPatch, watchdog_env: _FakeClock
) -> None:
    """The watchdog sets boot_failed when the deadline passes with the process still alive."""
    monkeypatch.setattr(pcsx2, "_pine_emu_status", lambda: None)
    monkeypatch.setattr(pcsx2.Pcsx2, "alive", lambda self: True)
    emu = pcsx2.Pcsx2()

    emu._boot_watchdog(1, emu._launch_seq)

    assert emu.boot_failed is True


def test_boot_watchdog_does_not_flag_a_process_that_already_exited(
    monkeypatch: pytest.MonkeyPatch, watchdog_env: _FakeClock
) -> None:
    """The watchdog does not flag a hang when the process has already exited."""
    monkeypatch.setattr(pcsx2, "_pine_emu_status", lambda: None)
    monkeypatch.setattr(pcsx2.Pcsx2, "alive", lambda self: False)
    emu = pcsx2.Pcsx2()

    emu._boot_watchdog(1, emu._launch_seq)

    assert emu.boot_failed is False


def test_boot_watchdog_abandons_a_superseded_launch(
    monkeypatch: pytest.MonkeyPatch, watchdog_env: _FakeClock
) -> None:
    """The watchdog gives up without flagging when a relaunch bumps the launch sequence."""
    emu = pcsx2.Pcsx2()
    seq = emu._launch_seq
    calls = {"n": 0}

    def status() -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            emu._launch_seq += 1  # a relaunch/stop landed mid-wait
        return None

    monkeypatch.setattr(pcsx2, "_pine_emu_status", status)
    monkeypatch.setattr(pcsx2.Pcsx2, "alive", lambda self: True)

    emu._boot_watchdog(1, seq)

    assert emu.boot_failed is False


def test_boot_watchdog_runs_and_can_flag_a_hang_with_no_resume_slot(
    monkeypatch: pytest.MonkeyPatch, watchdog_env: _FakeClock
) -> None:
    """The watchdog flags a hang with no resume slot and never attempts a load."""
    monkeypatch.setattr(pcsx2, "_pine_emu_status", lambda: None)
    monkeypatch.setattr(pcsx2.Pcsx2, "alive", lambda self: True)
    load_calls = []
    monkeypatch.setattr(pcsx2.Pcsx2, "load_state", lambda self, slot: load_calls.append(slot))
    emu = pcsx2.Pcsx2()

    emu._boot_watchdog(None, emu._launch_seq)

    assert emu.boot_failed is True
    assert load_calls == []


def test_launch_always_spawns_the_watchdog_even_with_no_resume_slot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A launch spawns the boot watchdog thread even when there is no resume slot.

    Regression test: ensures the 'if resume_slot is not None:' guard is never
    accidentally restored.
    """
    started = []
    monkeypatch.setattr(pcsx2, "_patch_ini", lambda: None)
    monkeypatch.setattr(pcsx2.Pcsx2, "_ensure_folder_card", lambda self: None)
    monkeypatch.setattr(pcsx2.Pcsx2, "_spawn", lambda self, cmd, env: None)

    def mock_thread(target: Any, args: tuple[Any, ...], daemon: bool) -> Any:
        """Capture Thread calls and record (target.__name__, args)."""
        started.append((target.__name__, args))
        return type("MockThread", (), {"start": lambda s: None})()

    monkeypatch.setattr(pcsx2, "Thread", mock_thread)
    emu = pcsx2.Pcsx2()

    emu.launch(tmp_path / "g.iso", None)

    assert len(started) == 1
    assert started[0][0] == "_boot_watchdog"
    assert started[0][1] == (None, emu._launch_seq)
