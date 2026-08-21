"""Save data in and out of the emulator's save directories.

Activate restores a zip archive into the emulator's save directories; exit
zips every save file modified since launch.
"""

import calendar
import io
import logging
import os
import time
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from . import settings

log = logging.getLogger(__name__)

SAVE_FILE_MAX_BYTES = int(os.environ.get("SAVE_FILE_MAX_BYTES", str(256 * 1024 * 1024)))
"""Env-tunable guard against runaway dumps, from `SAVE_FILE_MAX_BYTES` (default 256 MiB)."""
_SAVE_MTIME_SLACK = 2.0
"""Seconds of slack on the newer-file guard.

Zip stores mtimes at 2 s DOS resolution; the slack keeps the guard from
skipping files over rounding alone.
"""


def _iter_save_files(root: Path, subtrees: tuple[str, ...]) -> list[Path]:
    """List every regular file under the allowed subtrees.

    Sorted so identical content zips to identical bytes. Dot-prefixed
    components are staging or tmp entries and never ship, and symlinks are
    skipped.

    Args:
        root: The emulator's save data root.
        subtrees: Subdirectory names under `root` that hold save data.

    Returns:
        The files found, in sorted order.
    """
    files: list[Path] = []
    for sub in subtrees:
        base = root / sub
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*")):
            if not p.is_file() or p.is_symlink():
                continue
            rel = p.relative_to(base)
            if any(part.startswith(".") for part in rel.parts):
                continue
            # shadPS4 writes sce_sys/corrupted while a save is mounted
            # read-write and removes it on unmount; shipping it would make
            # the next mount treat the save as corrupt.
            if p.name == "corrupted" and p.parent.name == "sce_sys":
                continue
            files.append(p)
    return files


def _read_file_stable(p: Path, retries: int = 4, settle: float = 0.5) -> tuple[bytes, float] | None:
    """Read `p` only when size and mtime match before and after the read.

    This is what keeps a file the emulator is mid-writing from ever being
    shipped torn.

    Args:
        p: The file to read.
        retries: How many reads to attempt before giving up.
        settle: Seconds to wait between attempts.

    Returns:
        The file contents and its mtime, or None when the file could not be
        read or was still changing after every attempt (both are logged).
    """
    for attempt in range(retries):
        try:
            st_before = p.stat()
            data = p.read_bytes()
            st_after = p.stat()
        except OSError as exc:
            log.warning("saves: could not read %s: %s", p, exc)
            return None
        if (st_before.st_size, st_before.st_mtime_ns) == (
            st_after.st_size,
            st_after.st_mtime_ns,
        ):
            return data, st_after.st_mtime
        if attempt < retries - 1:
            time.sleep(settle)
    log.warning("saves: %s still being written, skipped", p)
    return None


def build_save_archive(
    root: Path, subtrees: tuple[str, ...], baseline: float
) -> dict[str, Any]:
    """Zip every save file modified since `baseline` (the launch timestamp).

    Member paths are relative to `root` and mtimes are stored in UTC on both
    sides, so a timezone difference between the dump and a later restore never
    shifts them past the newer-file guard.

    Args:
        root: The emulator's save data root.
        subtrees: Subdirectory names under `root` that hold save data.
        baseline: Unix timestamp; files with an mtime at or after it are included.

    Returns:
        A report dict of the shape
        `{"files": [{"path", "size", "mtime"}...], "skipped": n, "total_bytes": n,
        "zip_bytes": bytes | None, "error": str | None}`. `zip_bytes` is None when
        nothing changed or on error; `error` is set when the root is missing or
        the changed files exceed `SAVE_FILE_MAX_BYTES`.
    """
    report: dict = {"files": [], "skipped": 0, "total_bytes": 0, "zip_bytes": None, "error": None}
    if not root.is_dir():
        report["error"] = f"save data root missing: {root}"
        return report

    changed: list[Path] = []
    total = 0
    for p in _iter_save_files(root, subtrees):
        try:
            st = p.stat()
        except OSError:
            continue
        if st.st_mtime >= baseline:
            changed.append(p)
            total += st.st_size
    if not changed:
        return report
    if total > SAVE_FILE_MAX_BYTES:
        report["error"] = f"changed saves exceed size limit ({total} bytes)"
        return report

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in changed:
            result = _read_file_stable(p)
            if result is None:
                report["skipped"] += 1
                continue
            data, mtime = result
            # UTC on both sides so a TZ difference between the dump and a
            # later restore never shifts mtimes past the newer-file guard.
            info = zipfile.ZipInfo(
                p.relative_to(root).as_posix(),
                date_time=time.gmtime(mtime)[:6],
            )
            zf.writestr(info, data, zipfile.ZIP_DEFLATED)
            report["files"].append(
                {"path": p.relative_to(root).as_posix(), "size": len(data), "mtime": mtime}
            )
            report["total_bytes"] += len(data)
    if report["files"]:
        report["zip_bytes"] = buf.getvalue()
    return report


def _under(member: PurePosixPath, subtrees: tuple[str, ...]) -> bool:
    """Whether an archive member path lies inside one of the given subtrees.

    Args:
        member: The member path, relative to the save data root.
        subtrees: Subdirectory names to test against.

    Returns:
        True when `member` starts with one of the subtrees followed by a slash.
    """
    return any(member.as_posix().startswith(sub + "/") for sub in subtrees)


def extract_save_archive(
    content: bytes,
    root: Path,
    subtrees: tuple[str, ...],
    excluded: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Restore an archive into the emulator's data dir.

    `excluded` names subtrees the emulator owns but this session syncs some
    other way. Those members are dropped rather than refused: archives taken
    before that sync was turned on still carry them, and restoring one would
    undo what the other route just wrote. A member under neither is still a
    hard error, since that is the guard against an archive writing outside the
    save area.

    Existing files newer than their archive member are skipped so a restore
    can never roll back saves made since the archive was taken. Each file is
    written through a temp file and renamed into place.

    Args:
        content: The zip archive body.
        root: The emulator's save data root.
        subtrees: Subdirectory names under `root` that members may be restored into.
        excluded: Subdirectory names whose members are counted and dropped.

    Returns:
        A dict of the shape `{"written", "skipped", "excluded", "failed", "error"}`
        with counts for the first four and `error` set (and nothing written) when
        the body is not a zip, the archive is too large, or a member escapes the
        save dir or lies outside the subtrees.
    """
    result = {"written": 0, "skipped": 0, "excluded": 0, "failed": 0, "error": None}
    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile:
        result["error"] = "body is not a zip archive"
        return result
    with zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if sum(i.file_size for i in infos) > SAVE_FILE_MAX_BYTES:
            result["error"] = "archive exceeds size limit when extracted"
            return result
        wanted = []
        for info in infos:
            member = PurePosixPath(info.filename)
            if member.is_absolute() or ".." in member.parts:
                result["error"] = f"archive member escapes save dir: {info.filename}"
                return result
            if _under(member, excluded):
                result["excluded"] += 1
                continue
            if not _under(member, subtrees):
                result["error"] = f"archive member outside save subtrees: {info.filename}"
                return result
            wanted.append(info)

        for info in wanted:
            target = root / PurePosixPath(info.filename)
            mtime = calendar.timegm(info.date_time)
            try:
                if target.exists() and target.stat().st_mtime > mtime + _SAVE_MTIME_SLACK:
                    result["skipped"] += 1
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp = target.parent / f".{target.name}.tmp"
                tmp.write_bytes(zf.read(info))
                os.replace(tmp, target)
                os.utime(target, (mtime, mtime))
            except OSError as exc:
                log.warning("saves: could not restore %s: %s", info.filename, exc)
                result["failed"] += 1
                continue
            result["written"] += 1
    return result


def write_export(zip_bytes: bytes, name: str) -> str:
    """Persist a dump archive under `settings.EXPORT_DIR` for inspection.

    Args:
        zip_bytes: The archive body.
        name: The filename to write it as.

    Returns:
        The path written, as a string.
    """
    settings.EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = settings.EXPORT_DIR / name
    path.write_bytes(zip_bytes)
    return str(path)


def write_import(zip_bytes: bytes, name: str) -> str:
    """Persist an archive the parent uploaded for restore under `settings.IMPORT_DIR`.

    Written through a temp file and renamed so a half-received upload can
    never be handed to activate as a restore source.

    Args:
        zip_bytes: The archive body.
        name: The filename to write it as.

    Returns:
        The path written, as a string.
    """
    settings.IMPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = settings.IMPORT_DIR / name
    tmp = path.with_name(path.name + ".part")
    tmp.write_bytes(zip_bytes)
    tmp.replace(path)
    return str(path)
