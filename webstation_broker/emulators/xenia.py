"""Xenia Edge (Xbox 360) launcher: ROM resolution and launch/kill lifecycle.

Xenia has no save states and no external control API (no IPC, no socket, no
stdin protocol; the only listener is a gdbstub behind --debug, which drives
the JIT, not the session). Persistence is the game's own save data, which
the guest commits to plain host files under the content tree:

    <storage_root>/content/<XUID>/<TITLE_ID>/00000001/<save name>/...

Guest writes are write-through: every guest NtWriteFile is an immediate host
pwrite() with no user-space buffering, so save files are complete on disk
the moment the game writes them and survive a hard kill. Xenia installs no
SIGTERM/SIGINT handlers (only SIGILL/SIGSEGV/SIGBUS for the JIT), so SIGTERM
kills it with default disposition. That skips xenia's own exit path, but that
path only saves config and flushes the log, nothing save-related, so the base
SIGTERM->SIGKILL stop is the shutdown story. The exit must always be
broker-driven anyway: a game exiting to dashboard leaves xenia sitting on a
modal dialog instead of exiting, and a guest main-thread exit leaves the
process idling with the window open.

The profile tree (content/<XUID>/FFFE07D1/...) ships inside the content
subtree: Xbox 360 save paths embed the profile XUID, so profile and saves
must travel together for a restore into a fresh container to line up.

A profile has to exist before the broker launches anything. Xenia Edge
checks its account list as soon as the emulator is initialised and, finding
none, raises a native "No Profiles Found" dialog that is not gated on
--headless and that nobody in the stream can dismiss; without a profile a
game cannot save either. Creating one on the desktop signs it in and persists
the XUID into xenia-edge.config.toml under the storage root
(logged_profile_slot_0_xuid), so every later launch against the same
--storage_root signs in silently. docs/standalone_emulators.md carries the
user-facing version of this.

`--headless` does not mean windowless: the game still renders in the normal
window selkies captures. It auto-answers guest system dialogs (storage
select, sign-in, message boxes) that nobody in the stream could dismiss.
One title per process: loading a second game requires a process restart.

Bootable forms: an XISO (.iso), a bare executable (.xex), an extracted dump
(folder with default.xex at its root), or an XBLA / Games on Demand title
folder, whose STFS package the resolver digs out of the content-type layout
the console uses (see _find_container).
"""

import logging
import os
import re
from pathlib import Path
from typing import Optional

from .base import Emulator, base_launch_env

log = logging.getLogger(__name__)

ROM_ROOT = Path(os.environ.get("ROM_ROOT", "/romm"))

XENIA_BIN = os.environ.get("XENIA_BIN", "/opt/xenia/AppRun")
# Passed as --storage_root so saves land where the broker expects instead of
# ~/Xenia; content, cache, config and the signed-in profile all live under it.
# The desktop launcher has to point at the same directory, or the profile
# created there is not the one a broker launch boots with.
DATA_DIR = Path(os.environ.get("XENIA_DATA_DIR", "/config/xenia"))
XENIA_LOG_PATH = Path(os.environ.get("XENIA_LOG_PATH", "/config/xenia.log"))

# XISO dumps and extracted executables; a folder holding several candidates
# picks by this order. STFS containers (XBLA titles and Games on Demand
# installs) have no extension and are found by layout instead, see
# _find_container.
ROM_EXTENSIONS = (".iso", ".xex")
_ROM_SEARCH_GLOBS = ("*", "*/*")

# An XBLA or GoD title as the console lays it down, and as most dumps keep it:
#
#     [Content/<XUID>/]<TITLE_ID>/<CONTENT_TYPE>/<hash>
#
# <hash> is the STFS header package Xenia boots from; a GoD install keeps its
# payload beside it in <hash>.data/. Only these two content types are games
# (000D0000 arcade title, 00007000 installed game); DLC (00000002) and title
# updates (000B0000) in a sibling folder are picked up by Xenia from the
# content tree, not booted. The globs cover the title folder handed over bare
# (<TITLE_ID> as the root), with the Content/<XUID> prefix, and with one more
# wrapper such as the folder RomM names after the game.
_CONTAINER_TYPE_DIRS = ("000D0000", "00007000")
_CONTAINER_GLOBS = tuple(
    f"{'*/' * depth}{type_dir}/*"
    for depth in (0, 1, 2, 3)
    for type_dir in _CONTAINER_TYPE_DIRS
)
_STFS_MAGICS = (b"CON ", b"LIVE", b"PIRS")
_DISC_RE = re.compile(r"(?:^|[^a-z0-9])(?:disc|disk|cd)[\s._-]*(\d+)", re.IGNORECASE)


def _pick_rom_file(candidates: list[Path], base: Path) -> Optional[Path]:
    ranked = []
    for p in candidates:
        if p.name.startswith("."):
            continue
        ext = p.suffix.lower()
        if ext not in ROM_EXTENSIONS:
            continue
        try:
            if not p.is_file():
                continue
            real = p.resolve()
            rel = p.relative_to(base)
        except (OSError, ValueError):
            continue
        if not real.is_relative_to(ROM_ROOT):
            continue
        ranked.append(
            (_disc_number(rel), ROM_EXTENSIONS.index(ext), len(rel.parts), p.name.lower(), real)
        )
    if not ranked:
        return None
    return min(ranked)[4]


def _disc_number(rel: Path) -> int:
    match = _DISC_RE.search(str(rel))
    if match is None:
        return 1
    return max(1, int(match.group(1)))


def _is_stfs_package(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            return fh.read(4) in _STFS_MAGICS
    except OSError:
        return False


def _find_container(base: Path) -> Optional[Path]:
    """The STFS header package under a title folder, or None.

    The magic check is what separates the package from its .data payload
    directory and from anything else a dump might have dropped next to it.
    Shallowest match wins, then the arcade type over the installed-game type,
    so a folder holding both lands on one answer every time.
    """
    ranked = []
    for pattern in _CONTAINER_GLOBS:
        try:
            matches = list(base.glob(pattern))
        except OSError:
            return None
        for p in matches:
            if p.name.startswith("."):
                continue
            try:
                if not p.is_file():
                    continue
                real = p.resolve()
                rel = p.relative_to(base)
            except (OSError, ValueError):
                continue
            if not real.is_relative_to(ROM_ROOT) or not _is_stfs_package(real):
                continue
            ranked.append(
                (len(rel.parts), _CONTAINER_TYPE_DIRS.index(p.parent.name), p.name.lower(), real)
            )
    if not ranked:
        return None
    return min(ranked)[3]


class Xenia(Emulator):
    """Emulator adapter for Xenia Edge (Xbox 360)."""

    name = "xenia"
    display_name = "Xenia"
    save_root = DATA_DIR
    # The whole content tree: saves (<XUID>/<TITLE_ID>/00000001), their
    # .header sidecars, and the profile store the save paths are keyed by.
    # The delta dump only ships files modified since launch, so installed
    # DLC sitting in here does not bloat the archive.
    save_subtrees = ("content",)
    rom_extensions = ROM_EXTENSIONS
    log_path = XENIA_LOG_PATH
    # SIGTERM is already a hard kill (no handler installed) and save writes
    # are write-through, so the base 5 s before SIGKILL is only a formality.

    def resolve_rom_file(self, path: Path) -> Optional[Path]:
        """Resolve a library entry to the file Xenia should boot.

        A file is returned as given. A directory is resolved in order: an
        extracted dump's default.xex, then the best disc image or bare
        executable candidate, then an XBLA/Games on Demand content package.

        Args:
            path: The ROM library entry, either a bootable file or a
                directory to search.

        Returns:
            The path to boot, or None if nothing bootable was found or a
            default.xex symlink escapes ROM_ROOT.
        """
        if path.is_file():
            return path
        if not path.is_dir():
            return None
        # An extracted dump boots via its executable.
        default_xex = path / "default.xex"
        try:
            if default_xex.is_file():
                if default_xex.resolve().is_relative_to(ROM_ROOT):
                    return default_xex
                return None  # symlink escapes ROM_ROOT
            if default_xex.exists() or default_xex.is_symlink():
                # present but not a regular file: dangling symlink, or a
                # symlink to a directory/device/fifo. is_file() misses these,
                # and falling through to the candidate search would silently
                # boot something else instead of the dump's own executable.
                return None
        except OSError:
            return None
        candidates: list[Path] = []
        for pattern in _ROM_SEARCH_GLOBS:
            try:
                candidates.extend(path.glob(pattern))
            except OSError:
                return None
        rom = _pick_rom_file(candidates, path)
        if rom is not None:
            return rom
        # No disc or executable: an XBLA or GoD container, if the layout
        # holds one.
        return _find_container(path)

    def launch(self, rom_path: Path, resume_slot: Optional[int]) -> None:
        """Stop any running instance and launch Xenia against a ROM.

        Xenia has no save states, so resume_slot is accepted for interface
        parity with other emulators but only logged, never acted on; the
        game always resumes from its own save data.

        Args:
            rom_path: The resolved ROM file to boot.
            resume_slot: Ignored save-state slot, kept for interface parity.
        """
        self.stop()
        if resume_slot is not None:
            log.info(
                "xenia has no save states, resume_slot %s ignored "
                "(game resumes from its own save data)",
                resume_slot,
            )
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        log.info("launching xenia (rom=%s)", rom_path)
        self._spawn(
            [
                XENIA_BIN,
                "--fullscreen",
                "--headless",
                f"--storage_root={DATA_DIR}",
                "--discord=false",
                str(rom_path),
            ],
            base_launch_env(),
        )
