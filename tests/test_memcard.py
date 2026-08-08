"""Whole memory-card capture and replace."""

import io
import zipfile

from webstation_broker import memcard

MARKER = "_pcsx2_superblock"


def _zip(members: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in members.items():
            zf.writestr(name, content)
    return buf.getvalue()


def _names(archive: bytes) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        return sorted(zf.namelist())


def test_ensure_card_lays_down_the_marker_the_emulator_looks_for(tmp_path):
    card = tmp_path / "Slot 1"

    memcard.ensure_card(card, MARKER)

    assert (card / MARKER).is_file()
    assert (card / MARKER).stat().st_size == 0


def test_ensure_card_leaves_an_existing_marker_alone(tmp_path):
    card = tmp_path / "Slot 1"
    card.mkdir()
    (card / MARKER).write_bytes(b"formatted")

    memcard.ensure_card(card, MARKER)

    assert (card / MARKER).read_bytes() == b"formatted"


def test_capture_reports_no_card_when_the_slot_is_empty(tmp_path):
    assert memcard.build_archive(tmp_path / "missing", MARKER) is None


def test_capture_reports_no_card_when_only_the_scaffolding_is_there(tmp_path):
    card = tmp_path / "Slot 1"
    memcard.ensure_card(card, MARKER)

    assert memcard.build_archive(card, MARKER) is None


def test_capture_reports_a_card_once_the_emulator_has_formatted_it(tmp_path):
    card = tmp_path / "Slot 1"
    memcard.ensure_card(card, MARKER)
    (card / MARKER).write_bytes(b"superblock")
    (card / "BASCUS-97129").mkdir()
    (card / "BASCUS-97129" / "icon.sys").write_bytes(b"icon")

    archive = memcard.build_archive(card, MARKER)

    assert _names(archive) == ["BASCUS-97129/icon.sys", MARKER]


def test_capture_refuses_a_single_file_card(tmp_path):
    card = tmp_path / "Mcd001.ps2"
    card.write_bytes(b"8mb image")

    assert memcard.build_archive(card, MARKER) == memcard._FILE_CARD_ERROR


def test_replace_wipes_the_previous_player_card(tmp_path):
    card = tmp_path / "Slot 1"
    card.mkdir()
    (card / "BALEFT-BEHIND").mkdir()
    (card / "BALEFT-BEHIND" / "save.bin").write_bytes(b"previous player")

    written = memcard.replace(card, _zip({"BMINE-00001/save.bin": b"mine"}), MARKER)

    assert written == 1
    assert not (card / "BALEFT-BEHIND").exists()
    assert (card / "BMINE-00001" / "save.bin").read_bytes() == b"mine"


def test_replace_lays_the_marker_down_with_the_image(tmp_path):
    card = tmp_path / "Slot 1"

    memcard.replace(card, _zip({"BMINE-00001/save.bin": b"mine"}), MARKER)

    assert (card / MARKER).is_file()


def test_replace_keeps_a_marker_carried_by_the_image(tmp_path):
    card = tmp_path / "Slot 1"

    memcard.replace(card, _zip({MARKER: b"formatted"}), MARKER)

    assert (card / MARKER).read_bytes() == b"formatted"


def test_replace_refuses_a_body_that_is_not_a_zip(tmp_path):
    card = tmp_path / "Slot 1"
    memcard.ensure_card(card, MARKER)

    assert memcard.replace(card, b"not a zip", MARKER) == "body is not a zip archive"
    assert card.is_dir()


def test_replace_refuses_a_member_that_escapes_the_card(tmp_path):
    card = tmp_path / "Slot 1"
    card.mkdir()
    (card / "keep.bin").write_bytes(b"still here")

    result = memcard.replace(card, _zip({"../escaped.bin": b"x"}), MARKER)

    assert "escapes the card dir" in result
    assert (card / "keep.bin").read_bytes() == b"still here"
    assert not (tmp_path / "escaped.bin").exists()


def test_replace_refuses_a_single_file_card(tmp_path):
    card = tmp_path / "Mcd001.ps2"
    card.write_bytes(b"8mb image")

    assert memcard.replace(card, _zip({"a.bin": b"x"}), MARKER) == memcard._FILE_CARD_ERROR


def test_replace_leaves_no_staging_or_backup_behind(tmp_path):
    card = tmp_path / "Slot 1"
    memcard.ensure_card(card, MARKER)

    memcard.replace(card, _zip({"BMINE-00001/save.bin": b"mine"}), MARKER)

    assert sorted(p.name for p in tmp_path.iterdir()) == ["Slot 1"]


def test_a_replaced_card_captures_back_to_the_same_members(tmp_path):
    card = tmp_path / "Slot 1"
    image = _zip({MARKER: b"superblock", "BMINE-00001/save.bin": b"mine"})

    memcard.replace(card, image, MARKER)

    assert _names(memcard.build_archive(card, MARKER)) == _names(image)
