"""Whole memory-card sync: an emulator's Slot-1 card as one owned image.

Distinct from the archive path in saves.py, which ships only what a session
changed. A card travels whole, so a user's card can be laid down on any pooled
container and the previous player's saves never survive into the next session.
The card is a directory (a folder card), which is what makes it zippable and
replaceable as an image; a single-file card is refused rather than mangled.
"""

import io
import logging
import os
import shutil
import zipfile
from pathlib import Path, PurePosixPath
from threading import Lock

from . import settings
from .saves import SAVE_FILE_MAX_BYTES

log = logging.getLogger(__name__)

# Serializes whole-card operations. Staging and backup paths are derived from
# the card name alone, so two concurrent replaces would rmtree each other
# mid-write.
LOCK = Lock()

_FILE_CARD_ERROR = "slot 1 holds a single-file memory card, a folder card is required"


def _card_files(card: Path) -> list[Path]:
    return [p for p in sorted(card.rglob("*")) if p.is_file() and not p.is_symlink()]


def _is_blank_marker(path: Path, card: Path, marker: str | None) -> bool:
    """True for the marker file while it is still the empty one we laid down."""
    if marker is None or path != card / marker:
        return False
    try:
        return path.stat().st_size == 0
    except OSError:
        return False


def ensure_card(card: Path, marker: str | None) -> None:
    """Have a card the emulator will actually open waiting at `card`.

    A bare directory is not enough. PCSX2 skips any directory in its memcards
    folder that carries no marker file, so the slot reads as missing and the
    game has nowhere to save. The marker goes down empty, which is exactly what
    PCSX2 writes when it creates a folder card, and stays empty until the card
    is formatted.
    """
    card.mkdir(parents=True, exist_ok=True)
    if marker:
        (card / marker).touch(exist_ok=True)


def build_archive(card: Path, marker: str | None = None) -> bytes | None | str:
    """Zip the whole card at `card`, members relative to the card root.

    Relative so the image carries no trace of the card's name and can be laid
    down on a container that calls its card something else. Returns the zip
    bytes, None when the slot holds no card, or an error string.
    """
    if card.exists() and not card.is_dir():
        return _FILE_CARD_ERROR
    if not card.is_dir():
        return None
    files = _card_files(card)
    if all(_is_blank_marker(p, card, marker) for p in files):
        # Nothing here but the scaffolding the broker put down, so the emulator
        # never formatted a card into it. Reporting that as a card would have
        # RomM storing an empty image and prompting the user to import it; an
        # empty slot is what it actually is.
        return None
    total = 0
    for p in files:
        try:
            total += p.stat().st_size
        except OSError:
            continue
    if total > SAVE_FILE_MAX_BYTES:
        log.warning("memcard: card exceeds size limit (%d bytes)", total)
        return "memory card exceeds size limit"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in files:
            try:
                zf.write(p, p.relative_to(card).as_posix())
            except OSError as exc:
                log.warning("memcard: could not read %s: %s", p, exc)
    return buf.getvalue()


def replace(card: Path, content: bytes, marker: str | None = None) -> int | str:
    """Wipe the card at `card` and lay the pulled image down in its place.

    The whole card is replaced with no per-file merge: that is what isolates
    the next player on a pooled container from the last one. Extraction goes to
    a staging directory that is swapped over the live card, so a failure part
    way through never leaves a half-wiped card. Returns the file count written,
    or an error string.
    """
    if card.exists() and not card.is_dir():
        return _FILE_CARD_ERROR
    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile:
        return "body is not a zip archive"
    with zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if sum(i.file_size for i in infos) > SAVE_FILE_MAX_BYTES:
            return "archive exceeds size limit when extracted"
        if len(infos) > settings.SAVE_FILE_MAX_ENTRIES:
            return f"archive holds more than {settings.SAVE_FILE_MAX_ENTRIES} entries"
        for info in infos:
            member = PurePosixPath(info.filename)
            if member.is_absolute() or ".." in member.parts:
                return f"archive member escapes the card dir: {info.filename}"

        parent = card.parent
        staging = parent / f".{card.name}.new"
        backup = parent / f".{card.name}.old"
        shutil.rmtree(staging, ignore_errors=True)
        written = 0
        try:
            staging.mkdir(parents=True)
            # Down before the members so the card is openable the instant it is
            # swapped in, and so a wipe leaves a card rather than a directory
            # the emulator ignores. An image carrying its own marker wins.
            if marker:
                (staging / marker).touch()
            staging_real = staging.resolve()
            for info in infos:
                target = staging / PurePosixPath(info.filename)
                # Belt-and-suspenders on top of the member-path check above:
                # confirms the resolved write location is still under the
                # (still-empty, pre-swap) staging dir.
                if not target.parent.resolve().is_relative_to(staging_real):
                    raise ValueError(f"archive member resolves outside staging dir: {info.filename}")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(zf.read(info))
                written += 1
            # Move the old card aside, move the new one in, then drop the old.
            # Both live under the same parent, so the renames are atomic.
            if card.exists():
                os.replace(card, backup)
            os.replace(staging, card)
        except (OSError, ValueError) as exc:
            shutil.rmtree(staging, ignore_errors=True)
            if not card.exists() and backup.exists():
                try:
                    os.replace(backup, card)
                except OSError:
                    # The backup is now the only copy of the card, so it stays
                    # on disk for manual recovery instead of being deleted.
                    log.error(
                        "memcard: could not restore the card to %s, old card kept at %s",
                        card,
                        backup,
                    )
                    return f"could not write the memory card: {exc}"
            shutil.rmtree(backup, ignore_errors=True)
            return f"could not write the memory card: {exc}"
        shutil.rmtree(backup, ignore_errors=True)
    return written
