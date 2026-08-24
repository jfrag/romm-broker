"""Whole memory-card capture and replace.

Covers the scaffolding marker, archive capture, and the wholesale replace of a directory-backed
card.
"""

import io
import zipfile
from pathlib import Path

import pytest

from webstation_broker import memcard

MARKER = "_pcsx2_superblock"


def _zip(members: dict[str, bytes]) -> bytes:
    """Build an in-memory zip archive from a name-to-content mapping.

    Args:
        members: Archive member names mapped to their bytes.

    Returns:
        The zip file contents.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in members.items():
            zf.writestr(name, content)
    return buf.getvalue()


def _names(archive: bytes) -> list[str]:
    """List the member names of a zip archive in sorted order.

    Args:
        archive: The zip file contents.

    Returns:
        The sorted member names.
    """
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        return sorted(zf.namelist())


def test_ensure_card_lays_down_the_marker_the_emulator_looks_for(tmp_path: Path) -> None:
    """Ensure card lays down the marker the emulator looks for."""
    card = tmp_path / "Slot 1"

    memcard.ensure_card(card, MARKER)

    assert (card / MARKER).is_file()
    assert (card / MARKER).stat().st_size == 0


def test_ensure_card_leaves_an_existing_marker_alone(tmp_path: Path) -> None:
    """Ensure card leaves an existing marker alone."""
    card = tmp_path / "Slot 1"
    card.mkdir()
    (card / MARKER).write_bytes(b"formatted")

    memcard.ensure_card(card, MARKER)

    assert (card / MARKER).read_bytes() == b"formatted"


def test_capture_reports_no_card_when_the_slot_is_empty(tmp_path: Path) -> None:
    """Capture reports no card when the slot is empty."""
    assert memcard.build_archive(tmp_path / "missing", MARKER) is None


def test_capture_reports_no_card_when_only_the_scaffolding_is_there(tmp_path: Path) -> None:
    """Capture reports no card when only the scaffolding is there."""
    card = tmp_path / "Slot 1"
    memcard.ensure_card(card, MARKER)

    assert memcard.build_archive(card, MARKER) is None


def test_capture_reports_a_card_once_the_emulator_has_formatted_it(tmp_path: Path) -> None:
    """Capture reports a card once the emulator has formatted it."""
    card = tmp_path / "Slot 1"
    memcard.ensure_card(card, MARKER)
    (card / MARKER).write_bytes(b"superblock")
    (card / "BASCUS-97129").mkdir()
    (card / "BASCUS-97129" / "icon.sys").write_bytes(b"icon")

    archive = memcard.build_archive(card, MARKER)

    assert _names(archive) == ["BASCUS-97129/icon.sys", MARKER]


def test_capture_refuses_a_single_file_card(tmp_path: Path) -> None:
    """Capture refuses a single-file card."""
    card = tmp_path / "Mcd001.ps2"
    card.write_bytes(b"8mb image")

    assert memcard.build_archive(card, MARKER) == memcard._FILE_CARD_ERROR


def test_replace_wipes_the_previous_player_card(tmp_path: Path) -> None:
    """Replace wipes the previous player's card."""
    card = tmp_path / "Slot 1"
    card.mkdir()
    (card / "BALEFT-BEHIND").mkdir()
    (card / "BALEFT-BEHIND" / "save.bin").write_bytes(b"previous player")

    written = memcard.replace(card, _zip({"BMINE-00001/save.bin": b"mine"}), MARKER)

    assert written == 1
    assert not (card / "BALEFT-BEHIND").exists()
    assert (card / "BMINE-00001" / "save.bin").read_bytes() == b"mine"


def test_replace_lays_the_marker_down_with_the_image(tmp_path: Path) -> None:
    """Replace lays the marker down with the image."""
    card = tmp_path / "Slot 1"

    memcard.replace(card, _zip({"BMINE-00001/save.bin": b"mine"}), MARKER)

    assert (card / MARKER).is_file()


def test_replace_keeps_a_marker_carried_by_the_image(tmp_path: Path) -> None:
    """Replace keeps a marker carried by the image."""
    card = tmp_path / "Slot 1"

    memcard.replace(card, _zip({MARKER: b"formatted"}), MARKER)

    assert (card / MARKER).read_bytes() == b"formatted"


def test_replace_refuses_a_body_that_is_not_a_zip(tmp_path: Path) -> None:
    """Replace refuses a body that is not a zip."""
    card = tmp_path / "Slot 1"
    memcard.ensure_card(card, MARKER)

    assert memcard.replace(card, b"not a zip", MARKER) == "body is not a zip archive"
    assert card.is_dir()


def test_replace_refuses_a_member_that_escapes_the_card(tmp_path: Path) -> None:
    """Replace refuses a member that escapes the card."""
    card = tmp_path / "Slot 1"
    card.mkdir()
    (card / "keep.bin").write_bytes(b"still here")

    result = memcard.replace(card, _zip({"../escaped.bin": b"x"}), MARKER)

    assert "escapes the card dir" in result
    assert (card / "keep.bin").read_bytes() == b"still here"
    assert not (tmp_path / "escaped.bin").exists()


def test_replace_refuses_a_single_file_card(tmp_path: Path) -> None:
    """Replace refuses a single-file card."""
    card = tmp_path / "Mcd001.ps2"
    card.write_bytes(b"8mb image")

    assert memcard.replace(card, _zip({"a.bin": b"x"}), MARKER) == memcard._FILE_CARD_ERROR


def test_replace_leaves_no_staging_or_backup_behind(tmp_path: Path) -> None:
    """Replace leaves no staging or backup behind."""
    card = tmp_path / "Slot 1"
    memcard.ensure_card(card, MARKER)

    memcard.replace(card, _zip({"BMINE-00001/save.bin": b"mine"}), MARKER)

    assert sorted(p.name for p in tmp_path.iterdir()) == ["Slot 1"]


def test_replace_refuses_an_archive_with_too_many_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replace refuses an archive with more than SAVE_FILE_MAX_ENTRIES entries."""
    from webstation_broker import settings

    monkeypatch.setattr(settings, "SAVE_FILE_MAX_ENTRIES", 2)
    card = tmp_path / "Slot 1"
    content = _zip({"a.bin": b"1", "b.bin": b"2", "c.bin": b"3"})

    result = memcard.replace(card, content, MARKER)

    assert "more than 2 entries" in result
    assert not card.exists()


def test_a_replaced_card_captures_back_to_the_same_members(tmp_path: Path) -> None:
    """A replaced card captures back to the same members."""
    card = tmp_path / "Slot 1"
    image = _zip({MARKER: b"superblock", "BMINE-00001/save.bin": b"mine"})

    memcard.replace(card, image, MARKER)

    assert _names(memcard.build_archive(card, MARKER)) == _names(image)
