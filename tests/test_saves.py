"""Save archive build and restore.

Covers the delta archive built at exit, the restore run at activate, and the import and export
writers.
"""

import io
import os
import time
import zipfile
from pathlib import Path
from typing import Optional

import pytest

from webstation_broker import saves

# Zip entries carry a DOS timestamp, which has no room for anything before
# 1980, so the fixture clock sits in 2020 rather than at the epoch.
OLD = 1_600_000_000
BASELINE = OLD + 2000
NEW = OLD + 3000


def _write(path: Path, content: bytes = b"data", mtime: Optional[float] = None) -> Path:
    """Write a file, creating its parents and optionally pinning its mtime.

    Args:
        path: Where to write.
        content: The bytes to write.
        mtime: Unix time to stamp on the file, or None to leave the clock alone.

    Returns:
        The path written.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _zip(
    members: dict[str, bytes], when: tuple[int, int, int, int, int, int] = (2020, 1, 1, 0, 0, 0)
) -> bytes:
    """Build an in-memory zip archive whose entries all carry one timestamp.

    Args:
        members: Archive member names mapped to their bytes.
        when: The date_time tuple stamped on every entry.

    Returns:
        The zip file contents.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in members.items():
            zf.writestr(zipfile.ZipInfo(name, date_time=when), content)
    return buf.getvalue()


def test_build_ships_only_files_written_since_the_baseline(tmp_path: Path) -> None:
    """Build ships only the files written since the baseline."""
    _write(tmp_path / "memcards" / "old.bin", b"old", mtime=OLD)
    _write(tmp_path / "memcards" / "new.bin", b"new", mtime=NEW)

    report = saves.build_save_archive(tmp_path, ("memcards",), baseline=BASELINE)

    assert [f["path"] for f in report["files"]] == ["memcards/new.bin"]
    assert report["total_bytes"] == 3
    with zipfile.ZipFile(io.BytesIO(report["zip_bytes"])) as zf:
        assert zf.namelist() == ["memcards/new.bin"]


def test_build_ignores_subtrees_it_was_not_given(tmp_path: Path) -> None:
    """Build ignores subtrees it was not given."""
    _write(tmp_path / "memcards" / "card.bin", mtime=NEW)
    _write(tmp_path / "elsewhere" / "secret.bin", mtime=NEW)

    report = saves.build_save_archive(tmp_path, ("memcards",), baseline=0)

    assert [f["path"] for f in report["files"]] == ["memcards/card.bin"]


def test_build_skips_dot_prefixed_entries(tmp_path: Path) -> None:
    """Build skips dot-prefixed entries."""
    _write(tmp_path / "sstates" / ".staging.tmp", mtime=NEW)
    _write(tmp_path / "sstates" / ".hidden" / "inside.bin", mtime=NEW)
    _write(tmp_path / "sstates" / "state.p2s", mtime=NEW)

    report = saves.build_save_archive(tmp_path, ("sstates",), baseline=0)

    assert [f["path"] for f in report["files"]] == ["sstates/state.p2s"]


def test_build_drops_the_shadps4_corrupted_marker(tmp_path: Path) -> None:
    """Build drops the shadPS4 corrupted marker."""
    _write(tmp_path / "savedata" / "CUSA00001" / "sce_sys" / "corrupted", b"", mtime=NEW)
    _write(tmp_path / "savedata" / "CUSA00001" / "save.bin", mtime=NEW)

    report = saves.build_save_archive(tmp_path, ("savedata",), baseline=0)

    assert [f["path"] for f in report["files"]] == ["savedata/CUSA00001/save.bin"]


def test_build_refuses_a_dump_over_the_size_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Build refuses a dump over the size limit."""
    monkeypatch.setattr(saves, "SAVE_FILE_MAX_BYTES", 8)
    _write(tmp_path / "sstates" / "big.p2s", b"x" * 16, mtime=NEW)

    report = saves.build_save_archive(tmp_path, ("sstates",), baseline=0)

    assert report["zip_bytes"] is None
    assert "size limit" in report["error"]


def test_build_reports_a_missing_save_root(tmp_path: Path) -> None:
    """Build reports a missing save root."""
    report = saves.build_save_archive(tmp_path / "gone", ("sstates",), baseline=0)

    assert report["zip_bytes"] is None
    assert "save data root missing" in report["error"]


def test_build_produces_nothing_when_the_session_wrote_nothing(tmp_path: Path) -> None:
    """Build produces nothing when the session wrote nothing."""
    _write(tmp_path / "memcards" / "card.bin", mtime=OLD)

    report = saves.build_save_archive(tmp_path, ("memcards",), baseline=BASELINE)

    assert report["files"] == []
    assert report["zip_bytes"] is None
    assert report["error"] is None


def test_archive_round_trips_through_a_restore(tmp_path: Path) -> None:
    """An archive round-trips through a restore."""
    source = tmp_path / "source"
    _write(source / "GC" / "card.raw", b"payload", mtime=NEW)
    report = saves.build_save_archive(source, ("GC",), baseline=0)

    target = tmp_path / "target"
    target.mkdir()
    result = saves.extract_save_archive(report["zip_bytes"], target, ("GC",))

    assert result == {"written": 1, "skipped": 0, "excluded": 0, "failed": 0, "error": None}
    assert (target / "GC" / "card.raw").read_bytes() == b"payload"


def test_restore_refuses_a_member_outside_the_save_subtrees(tmp_path: Path) -> None:
    """Restore refuses a member outside the save subtrees."""
    result = saves.extract_save_archive(_zip({"etc/passwd": b"x"}), tmp_path, ("GC",))

    assert "outside save subtrees" in result["error"]
    assert not (tmp_path / "etc").exists()


def test_restore_refuses_a_member_that_escapes_the_root(tmp_path: Path) -> None:
    """Restore refuses a member that escapes the root."""
    result = saves.extract_save_archive(_zip({"GC/../../out.bin": b"x"}), tmp_path, ("GC",))

    assert "escapes save dir" in result["error"]


def test_restore_refuses_a_body_that_is_not_a_zip(tmp_path: Path) -> None:
    """Restore refuses a body that is not a zip."""
    result = saves.extract_save_archive(b"not a zip", tmp_path, ("GC",))

    assert result["error"] == "body is not a zip archive"


def test_restore_passes_over_an_excluded_subtree(tmp_path: Path) -> None:
    """Restore passes over an excluded subtree."""
    content = _zip({"memcards/Slot1/card": b"stale", "sstates/state.p2s": b"keep"})

    result = saves.extract_save_archive(
        content, tmp_path, ("sstates",), excluded=("memcards",)
    )

    assert result["excluded"] == 1
    assert result["written"] == 1
    assert result["error"] is None
    assert not (tmp_path / "memcards").exists()


def test_restore_never_rolls_back_a_newer_save(tmp_path: Path) -> None:
    """Restore never rolls back a newer save."""
    existing = _write(tmp_path / "GC" / "card.raw", b"newer", mtime=time.time() + 60)
    old = (2020, 1, 1, 0, 0, 0)

    result = saves.extract_save_archive(_zip({"GC/card.raw": b"older"}, when=old), tmp_path, ("GC",))

    assert result["skipped"] == 1
    assert result["written"] == 0
    assert existing.read_bytes() == b"newer"


def test_restore_refuses_an_archive_too_large_to_unpack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Restore refuses an archive too large to unpack."""
    monkeypatch.setattr(saves, "SAVE_FILE_MAX_BYTES", 8)

    result = saves.extract_save_archive(_zip({"GC/card.raw": b"x" * 16}), tmp_path, ("GC",))

    assert "size limit" in result["error"]


def test_write_import_lands_the_archive_under_its_final_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Write import lands the archive under its final name."""
    from webstation_broker import settings

    monkeypatch.setattr(settings, "IMPORT_DIR", tmp_path / "imports")

    path = saves.write_import(b"PK-body", "session-1.zip")

    assert path == str(tmp_path / "imports" / "session-1.zip")
    assert (tmp_path / "imports" / "session-1.zip").read_bytes() == b"PK-body"
    # The staging file exists only mid-write; a leftover would be handed to a
    # later activate as a restore source.
    assert list((tmp_path / "imports").glob("*.part")) == []


def test_restore_refuses_an_archive_with_too_many_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Restore refuses an archive with more than SAVE_FILE_MAX_ENTRIES entries."""
    from webstation_broker import settings

    monkeypatch.setattr(settings, "SAVE_FILE_MAX_ENTRIES", 2)
    content = _zip({"GC/a.bin": b"1", "GC/b.bin": b"2", "GC/c.bin": b"3"})

    result = saves.extract_save_archive(content, tmp_path, ("GC",))

    assert "more than 2 entries" in result["error"]
    assert not (tmp_path / "GC").exists()


def test_restore_skips_a_member_whose_directory_resolves_outside_root(tmp_path: Path) -> None:
    """Restore skips a member whose directory resolves outside root via a symlink."""
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "root"
    (root / "GC").mkdir(parents=True)
    (root / "GC" / "linked").symlink_to(outside, target_is_directory=True)

    result = saves.extract_save_archive(
        _zip({"GC/linked/card.raw": b"x"}), root, ("GC",)
    )

    assert result["failed"] == 1
    assert result["written"] == 0
    assert not (outside / "card.raw").exists()


def test_write_export_persists_the_dump(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Write export persists the dump."""
    from webstation_broker import settings

    monkeypatch.setattr(settings, "EXPORT_DIR", tmp_path / "exports")

    path = saves.write_export(b"zip-body", "session-1.zip")

    assert (tmp_path / "exports" / "session-1.zip").read_bytes() == b"zip-body"
    assert path.endswith("session-1.zip")
