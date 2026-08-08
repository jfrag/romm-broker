"""PCSX2 state naming and slot resolution."""

import os
from pathlib import Path

import pytest

from webstation_broker.emulators import pcsx2


@pytest.fixture
def sstate_dir(monkeypatch, tmp_path):
    d = tmp_path / "sstates"
    d.mkdir()
    monkeypatch.setattr(pcsx2, "SSTATE_DIR", d)
    return d


def _touch(path: Path, mtime=None) -> Path:
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
def test_restamp_keeps_the_serial_and_rewrites_the_slot(filename, expected):
    assert pcsx2._restamp_slot(filename, 10) == expected


@pytest.mark.parametrize("filename", ["SLUS-20946.p2s", "SLUS-20946.10.sav", "card.bin", ""])
def test_restamp_refuses_anything_that_is_not_a_state_name(filename):
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
def test_slot_match_accepts_both_widths_pcsx2_writes(name, slot, expected):
    assert pcsx2._matches_slot(Path(name), slot) is expected


def test_working_slot_reads_the_newest_state_in_it(sstate_dir):
    _touch(sstate_dir / "SLUS-1.10.p2s", mtime=1000)
    newest = _touch(sstate_dir / "SLUS-2.10.p2s", mtime=3000)
    _touch(sstate_dir / "SLUS-3.02.p2s", mtime=9000)

    assert pcsx2.newest_state_for_slot(10) == newest


def test_working_slot_is_empty_when_it_holds_nothing(sstate_dir):
    _touch(sstate_dir / "SLUS-3.02.p2s")

    assert pcsx2.newest_state_for_slot(10) is None


def test_state_target_names_a_push_for_the_working_slot(sstate_dir, monkeypatch):
    monkeypatch.setattr(pcsx2, "STATE_SLOT", 10)

    target = pcsx2.Pcsx2().state_target("SLUS-20946 (7D3A8B4E).03.p2s")

    assert target == sstate_dir / "SLUS-20946 (7D3A8B4E).10.p2s"


def test_state_target_refuses_another_disc_over_the_state_in_the_slot(sstate_dir, monkeypatch):
    monkeypatch.setattr(pcsx2, "STATE_SLOT", 10)
    existing = _touch(sstate_dir / "SLUS-20946 (7D3A8B4E).10.p2s")

    assert pcsx2.Pcsx2().state_target("SLUS-20946 (7D3A8B4E).01.p2s") == existing
    assert pcsx2.Pcsx2().state_target("SLES-51234 (00000000).01.p2s") is None


@pytest.mark.parametrize("filename", ["../escape.01.p2s", "", ".", "..", "card.bin"])
def test_state_target_refuses_a_name_pcsx2_would_never_write(sstate_dir, filename):
    assert pcsx2.Pcsx2().state_target(filename) is None


def test_clearing_the_slot_leaves_the_other_slots_alone(sstate_dir, monkeypatch):
    monkeypatch.setattr(pcsx2, "STATE_SLOT", 10)
    stale = _touch(sstate_dir / "SLUS-20946.10.p2s")
    other = _touch(sstate_dir / "SLUS-20946.02.p2s")

    pcsx2.Pcsx2().clear_working_slot()

    assert not stale.exists()
    assert other.exists()


def test_the_card_the_whole_card_routes_sync_is_the_slot_1_folder():
    emu = pcsx2.Pcsx2()

    assert emu.memory_card_path().parent == pcsx2.MEMCARD_DIR
    # The card rides the memory-card routes, so activate has to be able to take
    # it back out of the save archive by name.
    assert emu.memory_card_subtree in emu.save_subtrees
    assert emu.memory_card_marker
