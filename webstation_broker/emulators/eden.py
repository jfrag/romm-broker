"""Eden (Nintendo Switch) launcher: ROM resolution, qt-config.ini patching, and SIGTERM shutdown.

Eden has no save states and no external control API. Persistence is the
game's own save data, which the emulated game commits directly to host files
under the virtual NAND (`nand/user/save/...`). Save paths are keyed by the
Switch profile UUID, so the profile store (`nand/system/save/8000000000000010`)
ships with the saves; that way a save archive restored into a fresh
container brings its matching profile along and the paths line up.

Shutdown: Eden's Qt frontend routes SIGTERM through the event loop into a
normal window close (graceful emulation teardown). SIGINT is `_exit(1)` in
Eden, so the broker never sends it. The close path pops a confirmation
dialog unless the `confirmStop` UI setting is Ask_Never, so that is patched
before every launch.
"""

import logging
import os
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Optional

from .base import Emulator, base_launch_env

log = logging.getLogger(__name__)

ROM_ROOT = Path(os.environ.get("ROM_ROOT", "/romm"))
"""Library root a resolved ROM must live under (env `ROM_ROOT`, default `/romm`)."""

CONFIG_DIR = Path(os.environ.get("EDEN_CONFIG_DIR", "/config/.config/eden"))
"""Eden's config directory (env `EDEN_CONFIG_DIR`, default `/config/.config/eden`)."""
DATA_DIR = Path(os.environ.get("EDEN_DATA_DIR", "/config/.local/share/eden"))
"""Eden's data directory holding the virtual NAND (env `EDEN_DATA_DIR`)."""
INI_PATH = CONFIG_DIR / "qt-config.ini"
"""The qt-config.ini patched before every launch."""
EDEN_LOG_PATH = Path(os.environ.get("EDEN_LOG_PATH", "/config/eden.log"))
"""The emulator log file (env `EDEN_LOG_PATH`, default `/config/eden.log`)."""

ROM_EXTENSIONS = (".xci", ".nsp", ".nca", ".nro")
"""Formats Eden's loader boots directly, best first; a folder holding several picks by this order."""
_ROM_SEARCH_GLOBS = ("*", "*/*")
"""Glob patterns a ROM folder is searched with, one level of wrapper folder deep."""
_ADDON_RE = re.compile(r"(?:^|[^a-z0-9])(?:update|upd|dlc|patch)(?:[^a-z0-9]|$)", re.IGNORECASE)
"""Matches update and DLC names: they sit beside base games in library folders, and the base game boots."""

_INI_PATCHES: dict[tuple[str, str], str] = {
    ("UI", "confirmStop\\default"): "confirmStop\\default = false",
    ("UI", "confirmStop"): "confirmStop = 2",
}
"""qt-config.ini lines forced before every launch, keyed `(section, key)`.

Eden serializes enum settings as their underlying integer;
ConfirmStop::Ask_Never is 2. The `\\default` flag must be false or the
stored value is ignored in favor of the built-in default (Ask_Always),
which pops a confirmation dialog on close and would hang a headless
SIGTERM shutdown.
"""


def _pick_rom_file(candidates: Iterable[Path], base: Path) -> Optional[Path]:
    """Pick the best bootable file among `candidates`.

    Hidden files, non-files and anything resolving outside `ROM_ROOT` are
    skipped. Ranking prefers base games over updates and DLC, then the
    `ROM_EXTENSIONS` order, then the shallowest path, then the lowercased name.

    Args:
        candidates: Paths found under `base` by the search globs.
        base: The directory the candidates were searched from.

    Returns:
        The resolved path of the best candidate, or None when nothing qualifies.
    """
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
        is_addon = 1 if _ADDON_RE.search(str(rel)) else 0
        ranked.append(
            (is_addon, ROM_EXTENSIONS.index(ext), len(rel.parts), p.name.lower(), real)
        )
    if not ranked:
        return None
    return min(ranked)[4]


def _patch_ini() -> None:
    """Force broker-required qt-config.ini settings before every launch.

    A missing file is seeded with just the patched section and Eden fills in
    the rest. Otherwise the file is patched line-wise so every other setting
    survives untouched.

    Raises:
        OSError: When the file cannot be read, written or replaced.
        UnicodeDecodeError: When the existing file is not decodable text.

    Both are fatal to a launch rather than a warning to launch past: an
    unpatched config leaves `confirmStop` on Ask_Always, and the modal that
    then answers SIGTERM holds the shutdown until the SIGKILL escalation cuts
    a running game off mid-save.
    """
    try:
        if not INI_PATH.exists():
            # First run: write a minimal config, Eden fills in the rest.
            INI_PATH.parent.mkdir(parents=True, exist_ok=True)
            INI_PATH.write_text(
                "[UI]\n" + "\n".join(_INI_PATCHES.values()) + "\n"
            )
            return
        lines = INI_PATH.read_text().splitlines()
        section = ""
        applied: set[tuple[str, str]] = set()
        new_lines: list[str] = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                section = stripped[1:-1]
                new_lines.append(line)
                continue
            matched = False
            for (sec, key), val in _INI_PATCHES.items():
                if section != sec:
                    continue
                if stripped.startswith(f"{key} =") or stripped.startswith(f"{key}="):
                    new_lines.append(val)
                    applied.add((sec, key))
                    matched = True
                    break
            if not matched:
                new_lines.append(line)
        missing = [(s, k, v) for (s, k), v in _INI_PATCHES.items() if (s, k) not in applied]
        if missing:
            present = {
                ln.strip()[1:-1]
                for ln in new_lines
                if ln.strip().startswith("[") and ln.strip().endswith("]")
            }
            for sec, _key, val in missing:
                if sec in present:
                    out: list[str] = []
                    inserted = False
                    for ln in new_lines:
                        out.append(ln)
                        if not inserted and ln.strip() == f"[{sec}]":
                            out.append(val)
                            inserted = True
                    new_lines = out
                else:
                    new_lines.extend(["", f"[{sec}]", val])
                    present.add(sec)
        tmp = INI_PATH.with_suffix(".tmp")
        tmp.write_text("\n".join(new_lines) + "\n")
        tmp.replace(INI_PATH)
    except (OSError, UnicodeDecodeError):
        log.exception("eden: qt-config.ini patch failed at %s, refusing to launch", INI_PATH)
        raise


class Eden(Emulator):
    """Nintendo Switch via Eden, driven by command line flags and a graceful SIGTERM.

    Eden has no external control API, so the broker patches qt-config.ini
    before every launch (close confirmation off) and boots fullscreen with
    `-f -g`. A patch that fails aborts the launch: the close confirmation is
    what the whole shutdown path rests on. SIGTERM goes through Eden's Qt
    event loop into a normal window close, so the stop is a graceful emulation
    teardown; SIGINT is never used because Eden maps it to `_exit(1)`.

    There are no save states: persistence is the game's own save data under
    the virtual NAND, and the archive carries it together with the profile
    store, since Switch save paths embed the profile UUID. A resume slot is
    logged and ignored.

    Attributes:
        name: Provider key, `eden`.
        display_name: Human-readable name.
        save_root: The data directory the save subtrees hang off.
        save_subtrees: Game saves plus the profile store.
        rom_extensions: Bootable formats, best first.
        log_path: The emulator log file.
        term_timeout: SIGTERM grace before SIGKILL (env `EDEN_STOP_WAIT`, default 15).
    """

    name = "eden"
    display_name = "Eden"
    save_root = DATA_DIR
    save_subtrees = ("nand/user/save", "nand/system/save/8000000000000010")
    """Game saves plus the profile store.

    Switch save paths embed the profile UUID, so the two must travel
    together for restored saves to resolve.
    """
    rom_extensions = ROM_EXTENSIONS
    log_path = EDEN_LOG_PATH
    term_timeout = float(os.environ.get("EDEN_STOP_WAIT", "15"))
    """SIGTERM grace before SIGKILL (env `EDEN_STOP_WAIT`, default 15).

    A running game takes longer than the base 5 s to tear down gracefully;
    give SIGTERM room before escalating to SIGKILL.
    """

    def resolve_rom_file(self, path: Path) -> Optional[Path]:
        """The file Eden should boot for `path`.

        Args:
            path: A ROM file, or a folder searched up to two levels deep.

        Returns:
            The file itself, the best-ranked bootable file in the folder, or None.
        """
        if path.is_file():
            return path
        if not path.is_dir():
            return None
        candidates: list[Path] = []
        for pattern in _ROM_SEARCH_GLOBS:
            try:
                candidates.extend(path.glob(pattern))
            except OSError as exc:
                # One unreadable subdirectory must not discard what the other
                # patterns already found and report the title as unbootable.
                log.warning("eden: search of %s for %s failed: %s", path, pattern, exc)
        return _pick_rom_file(candidates, path)

    def launch(self, rom_path: Path, resume_slot: Optional[int]) -> None:
        """Patch qt-config.ini and boot the game fullscreen.

        Args:
            rom_path: The file to boot.
            resume_slot: Ignored with a log line; Eden has no save states.

        Raises:
            OSError: When qt-config.ini could not be patched, so nothing is
                spawned; see `_patch_ini`.
            UnicodeDecodeError: Likewise, for an undecodable existing file.
        """
        self.stop()
        _patch_ini()
        if resume_slot is not None:
            log.info(
                "eden has no save states, resume_slot %s ignored "
                "(game resumes from its own save data)",
                resume_slot,
            )
        binary = os.environ.get("EDEN_BIN", "eden")
        log.info("launching eden (rom=%s)", rom_path)
        self._spawn([binary, "-f", "-g", str(rom_path)], base_launch_env())
