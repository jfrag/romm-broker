"""Azahar (Nintendo 3DS) launcher: qt-config.ini patching and SIGTERM
shutdown.

Azahar has no control API reachable from outside the process. Its only
network surface is a UDP RPC server that reads and writes emulated memory,
with nothing for state or shutdown, and its command line takes a ROM path and
little else. So there is no mid-session save state here: persistence is the
game's own save data, written to host files under the emulated SD card and
NAND. Azahar installs no SIGTERM handler, so the stop is a hard kill and the
dump takes whatever the game had already committed to disk.

The 3DS data root is XDG-derived (`$XDG_DATA_HOME/azahar-emu`), with config
alongside it under `$XDG_CONFIG_HOME`. Azahar also has a portable mode that
triggers on a `user/` directory in the *working* directory, which is why the
launch never chdir's anywhere it might find one.

Save data paths are stable across containers: the console and SD card ids
Azahar files saves under are hardcoded to all-zeros rather than generated per
install, so a save dumped in one session restores into the next.
"""

import configparser
import logging
import os
import re
import time
from pathlib import Path

from .base import Emulator, base_launch_env

log = logging.getLogger(__name__)

ROM_ROOT = Path(os.environ.get("ROM_ROOT", "/romm"))


def _xdg_dir(var: str, fallback: str) -> str:
    """Azahar's Linux layout: one `azahar-emu` directory per XDG root."""
    xdg = os.environ.get(var)
    if xdg and os.path.isabs(xdg):
        return os.path.join(xdg, "azahar-emu")
    return os.path.join(os.environ.get("HOME", "/config"), fallback, "azahar-emu")


USER_DIR = Path(os.environ.get("AZAHAR_USER_DIR", _xdg_dir("XDG_DATA_HOME", ".local/share")))
CONFIG_DIR = Path(os.environ.get("AZAHAR_CONFIG_DIR", _xdg_dir("XDG_CONFIG_HOME", ".config")))
CONFIG_PATH = CONFIG_DIR / "qt-config.ini"
AZAHAR_LOG_PATH = Path(os.environ.get("AZAHAR_LOG_PATH", "/config/azahar.log"))

# The console and SD card ids Azahar files saves under. Both are fixed
# all-zeros rather than generated per install, so these paths are the same in
# every container and a dumped save restores where the next session looks.
SYSTEM_ID = "0" * 32
SDCARD_ID = "0" * 32

SDMC_DIR = USER_DIR / "sdmc" / "Nintendo 3DS" / SYSTEM_ID / SDCARD_ID
NAND_DATA_DIR = USER_DIR / "nand" / "data" / SYSTEM_ID

# Roots holding save trees, each keyed <titleHigh>/<titleLow>. SD title saves
# are where a game's own saves land; extdata carries the larger side data some
# games keep (photos, downloaded content), and sysdata the system's own.
_SAVE_GROUP_ROOTS = (
    SDMC_DIR / "title",
    SDMC_DIR / "extdata",
    NAND_DATA_DIR / "extdata",
    NAND_DATA_DIR / "sysdata",
)

# Formats Azahar boots directly, best first; a folder holding several
# candidates picks by this order. No .cia: that is an installable package
# rather than something the emulator boots.
ROM_EXTENSIONS = (
    ".3ds", ".cci", ".zcci", ".cxi", ".zcxi", ".app",
    ".3dsx", ".z3dsx", ".elf", ".axf", ".bin",
)
# A library folder wrapping a game adds a level, so search two deep.
_ROM_SEARCH_GLOBS = ("*", "*/*")
# Updates and DLC sit beside base games in library folders; boot the base game.
_ADDON_RE = re.compile(r"(?:^|[^a-z0-9])(?:update|upd|dlc|patch)(?:[^a-z0-9]|$)", re.IGNORECASE)

# Title dirs under a save group root are <titleHigh>/<titleLow>, both 8 hex.
_HEX8_RE = re.compile(r"^[0-9a-fA-F]{8}$")

# qt-config.ini keys forced before every launch, by ini section. Only the ones
# that would otherwise put something in front of the game: two that phone home
# and prompt, and the close confirmation that would leave a modal behind in a
# session nobody can click.
_CONFIG_PATCHES: dict[str, dict[str, str]] = {
    "UI": {
        "confirmClose": "false",
        "check_for_update_on_start": "false",
        "enable_discord_presence": "false",
    },
}


def _pick_rom_file(candidates, base: Path) -> Path | None:
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


def _patch_config() -> None:
    """Force broker-required qt-config.ini values before every launch.

    Patched key-wise, and with raw parsing, so everything else in the file
    survives: Azahar rewrites this whole file on exit, and it holds the
    player's own settings alongside QSettings-encoded keys that would not
    round-trip through interpolation.
    """
    try:
        parser = configparser.RawConfigParser()
        # Keys are case-sensitive here; the default would fold them and write
        # back a second, lowercased copy of every setting Azahar wrote.
        parser.optionxform = str
        if CONFIG_PATH.exists():
            try:
                parser.read(CONFIG_PATH, encoding="utf-8")
            except (configparser.Error, UnicodeDecodeError) as exc:
                log.warning("qt-config.ini unreadable (%s), reseeding it", exc)
                parser = configparser.RawConfigParser()
                parser.optionxform = str
        for section, entries in _CONFIG_PATCHES.items():
            if not parser.has_section(section):
                parser.add_section(section)
            for key, value in entries.items():
                parser.set(section, key, value)
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_PATH.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            # QSettings writes `key=value`; keep that shape so a player
            # reading the file sees one style throughout.
            parser.write(fh, space_around_delimiters=False)
        tmp.replace(CONFIG_PATH)
    except Exception:
        log.exception("qt-config.ini patch failed, broker settings NOT applied")


class Azahar(Emulator):
    name = "azahar"
    display_name = "Azahar"
    save_root = USER_DIR
    # Scoped to the save trees: the rest of the data root is config, cache,
    # shaders and system titles, none of which belong in a save archive.
    save_subtrees = (
        f"sdmc/Nintendo 3DS/{SYSTEM_ID}/{SDCARD_ID}/title",
        f"sdmc/Nintendo 3DS/{SYSTEM_ID}/{SDCARD_ID}/extdata",
        f"nand/data/{SYSTEM_ID}/extdata",
        f"nand/data/{SYSTEM_ID}/sysdata",
    )
    rom_extensions = ROM_EXTENSIONS
    log_path = AZAHAR_LOG_PATH
    # No SIGTERM handler: the default action ends the process at once, and
    # whatever the game committed is already on disk. The grace window only
    # covers process-group teardown.
    term_timeout = float(os.environ.get("AZAHAR_STOP_WAIT", "5"))

    def __init__(self):
        super().__init__()
        self._session_start = 0.0

    def prepare_restore(self) -> None:
        self.stop()

    def resolve_rom_file(self, path: Path) -> Path | None:
        if path.is_file():
            return path
        if not path.is_dir():
            return None
        candidates: list[Path] = []
        for pattern in _ROM_SEARCH_GLOBS:
            try:
                candidates.extend(path.glob(pattern))
            except OSError:
                return None
        return _pick_rom_file(candidates, path)

    def launch(self, rom_path: Path, resume_slot: int | None) -> None:
        self.stop()
        _patch_config()
        if resume_slot:
            log.info(
                "azahar has no reachable save states, resume_slot %s ignored "
                "(game resumes from its own save data)",
                resume_slot,
            )
        self._session_start = time.time()
        binary = os.environ.get("AZAHAR_BIN", "/opt/azahar/AppRun")
        # Windowed, not fullscreen: a fullscreen Azahar stops rendering the
        # moment the display resizes under it, and the display resizes
        # whenever the player resizes their browser window.
        cmd = [binary, "-w", str(rom_path)]
        log.info("launching azahar (rom=%s)", rom_path)
        self._spawn(cmd, base_launch_env())

    def _modified_title_saves(self) -> list[Path]:
        """Title save dirs holding a file written while the session ran."""
        selected: list[Path] = []
        for root in _SAVE_GROUP_ROOTS:
            if not root.is_dir():
                continue
            try:
                for high in sorted(root.iterdir()):
                    if not high.is_dir() or not _HEX8_RE.match(high.name):
                        continue
                    for title in sorted(high.iterdir()):
                        if not title.is_dir():
                            continue
                        try:
                            if any(
                                p.is_file() and p.stat().st_mtime >= self._session_start
                                for p in title.rglob("*")
                            ):
                                selected.append(title)
                        except OSError:
                            continue
            except OSError:
                continue
        return selected

    def save_and_exit(self, slot: int) -> dict:
        self.stop()
        # The dump ships files newer than the session baseline. A 3DS save is
        # a directory tree the game rewrites only partially, so refresh every
        # mtime in this session's title save dirs: they ship whole, other
        # titles' saves stay filtered out.
        now = time.time()
        for d in self._modified_title_saves():
            for p in d.rglob("*"):
                if p.is_file():
                    try:
                        os.utime(p, (now, now))
                    except OSError:
                        pass
        return {"state_saved": None, "state_slot": None, "state_file": None}
