"""Cemu (Wii U) launcher: settings.xml patching, gamepad profile seeding,
and SIGTERM shutdown.

Cemu has no control API and no save states. Persistence is the game's own
save data, written to host files under the virtual MLC
(`mlc01/usr/save/<titleHigh>/<titleLow>/user/<persistentId>`). Cemu installs
no signal handler, so SIGTERM is a hard kill; saves are already on disk.

A first launch with no settings.xml opens a modal Getting Started dialog, so
a minimal config is seeded before every launch. Cemu creates the default
account (0x80000001) itself on first boot, so save paths line up across
containers without the account store traveling.

Cemu applies its built-in controller mapping only through the GUI, so the
broker seeds a Wii U GamePad profile for player 0 bound to the selkies
virtual pad. Cemu addresses SDL controllers by joystick GUID, and SDL >= 2.24
folds a CRC16 of the device name into that GUID, so the profile carries one
controller node per GUID variant; the one that matches binds.
"""

import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from .base import Emulator, base_launch_env

log = logging.getLogger(__name__)

ROM_ROOT = Path(os.environ.get("ROM_ROOT", "/romm"))


def _xdg_dir(var: str, fallback: str) -> str:
    """Cemu's Linux layout: config under $XDG_CONFIG_HOME/Cemu, user data
    (the mlc) under $XDG_DATA_HOME/Cemu."""
    xdg = os.environ.get(var)
    if xdg and os.path.isabs(xdg):
        return os.path.join(xdg, "Cemu")
    return os.path.join(os.environ.get("HOME", "/config"), fallback, "Cemu")


CONFIG_DIR = Path(os.environ.get("CEMU_CONFIG_DIR", _xdg_dir("XDG_CONFIG_HOME", ".config")))
DATA_DIR = Path(os.environ.get("CEMU_DATA_DIR", _xdg_dir("XDG_DATA_HOME", ".local/share")))
SETTINGS_PATH = CONFIG_DIR / "settings.xml"
PROFILE_PATH = CONFIG_DIR / "controllerProfiles" / "controller0.xml"
MLC_DIR = Path(os.environ.get("CEMU_MLC_DIR", str(DATA_DIR / "mlc01")))
SAVE_DIR = MLC_DIR / "usr" / "save"
CEMU_LOG_PATH = Path(os.environ.get("CEMU_LOG_PATH", "/config/cemu.log"))

# Formats Cemu boots directly, best first; a folder holding several
# candidates picks by this order.
ROM_EXTENSIONS = (".wua", ".wux", ".wud", ".wuhb", ".iso", ".rpx", ".elf")
# An extracted dump boots from <game>/code/<title>.rpx, two levels down; a
# library folder wrapping one adds a third.
_ROM_SEARCH_GLOBS = ("*", "*/*", "*/*/*")
# Updates/DLC sit beside base games in library folders; boot the base game.
_ADDON_RE = re.compile(r"(?:^|[^a-z0-9])(?:update|upd|dlc|patch)(?:[^a-z0-9]|$)", re.IGNORECASE)

# Title save dirs are usr/save/<titleHigh>/<titleLow>; "system" holds the
# account store and play stats.
_HEX8_RE = re.compile(r"^[0-9a-fA-F]{8}$")

# settings.xml keys forced before every launch (all children of <content>).
# check_update phones home at startup; the rest keep the session free of
# dialogs and chrome.
_SETTINGS_PATCHES: dict[str, str] = {
    "check_update": "false",
    "receive_untested_updates": "false",
    "use_discord_presence": "false",
    "play_boot_sound": "false",
    "fullscreen_menubar": "false",
}

# The selkies virtual pad as the kernel xpad driver presents it.
_PAD_NAME = os.environ.get("CEMU_PAD_NAME", "Microsoft X-Box 360 pad")
_PAD_VENDOR = 0x045E
_PAD_PRODUCT = 0x028E
_PAD_VERSION = 0x0110

# Wii U GamePad mapping ids -> Cemu SDL button codes, Cemu's own default
# layout for a generic SDL pad: labels map by position (VPAD A is the east
# button), ZL/ZR are the analog trigger axes, sticks are axis half-ranges.
_VPAD_SDL_MAPPINGS: tuple[tuple[int, int], ...] = (
    (1, 1),    # A -> east
    (2, 0),    # B -> south
    (3, 3),    # X -> north
    (4, 2),    # Y -> west
    (5, 9),    # L -> left shoulder
    (6, 10),   # R -> right shoulder
    (7, 42),   # ZL -> left trigger axis
    (8, 43),   # ZR -> right trigger axis
    (9, 6),    # + -> start
    (10, 4),   # - -> back
    (11, 11),  # d-pad up
    (12, 12),  # d-pad down
    (13, 13),  # d-pad left
    (14, 14),  # d-pad right
    (15, 7),   # left stick click
    (16, 8),   # right stick click
    (17, 45),  # left stick up    -> axis Y-
    (18, 39),  # left stick down  -> axis Y+
    (19, 44),  # left stick left  -> axis X-
    (20, 38),  # left stick right -> axis X+
    (21, 47),  # right stick up    -> rotation Y-
    (22, 41),  # right stick down  -> rotation Y+
    (23, 46),  # right stick left  -> rotation X-
    (24, 40),  # right stick right -> rotation X+
    (27, 5),   # home -> guide
)


def _crc16(data: bytes) -> int:
    """CRC-16/ARC, the checksum SDL folds into a joystick GUID."""
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def _sdl_guid(crc: int) -> str:
    """A Linux evdev SDL joystick GUID: bus, name CRC, vendor, product and
    version as little-endian u16 fields padded to 16 bytes."""
    fields = (0x03, crc, _PAD_VENDOR, 0, _PAD_PRODUCT, 0, _PAD_VERSION, 0)
    return "".join(f"{f & 0xFF:02x}{(f >> 8) & 0xFF:02x}" for f in fields)


def _pad_uuids() -> list[str]:
    """The uuids the profile binds, in Cemu's `<index>_<guid>` form: one
    with the CRC field zero (SDL < 2.24) and one with the name hash."""
    override = os.environ.get("CEMU_PAD_UUIDS", "")
    if override.strip():
        return [u.strip() for u in override.split(",") if u.strip()]
    return [f"0_{_sdl_guid(0)}", f"0_{_sdl_guid(_crc16(_PAD_NAME.encode()))}"]


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


def _patch_settings() -> None:
    """Force broker-required settings.xml values before every launch.

    A missing file is seeded, which also skips the first-start Getting
    Started dialog. Patched key-wise so every other setting the user tuned
    through the GUI survives untouched."""
    try:
        root = None
        if SETTINGS_PATH.exists():
            try:
                root = ET.parse(SETTINGS_PATH).getroot()
            except ET.ParseError as exc:
                log.warning("settings.xml unreadable (%s), reseeding it", exc)
        if root is None or root.tag != "content":
            root = ET.Element("content")
        for key, value in _SETTINGS_PATCHES.items():
            node = root.find(key)
            if node is None:
                node = ET.SubElement(root, key)
            node.text = value
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = SETTINGS_PATH.with_suffix(".tmp")
        ET.ElementTree(root).write(tmp, encoding="UTF-8", xml_declaration=True)
        tmp.replace(SETTINGS_PATH)
    except Exception:
        log.exception("settings.xml patch failed, broker settings NOT applied")


def _pad_profile_xml() -> str:
    """The controller0.xml body: one Wii U GamePad with a controller node
    per candidate uuid, all carrying the same mapping. Cemu loads controller
    nodes independently; a node whose device never connects is inert."""
    root = ET.Element("emulated_controller")
    ET.SubElement(root, "type").text = "Wii U GamePad"
    for uuid in _pad_uuids():
        controller = ET.SubElement(root, "controller")
        ET.SubElement(controller, "api").text = "SDLController"
        ET.SubElement(controller, "uuid").text = uuid
        ET.SubElement(controller, "display_name").text = _PAD_NAME
        ET.SubElement(controller, "rumble").text = "0"
        for section in ("axis", "rotation", "trigger"):
            sec = ET.SubElement(controller, section)
            ET.SubElement(sec, "deadzone").text = "0.25"
            ET.SubElement(sec, "range").text = "1"
        mappings = ET.SubElement(controller, "mappings")
        for mapping, button in _VPAD_SDL_MAPPINGS:
            entry = ET.SubElement(mappings, "entry")
            ET.SubElement(entry, "mapping").text = str(mapping)
            ET.SubElement(entry, "button").text = str(button)
    ET.indent(root)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"


def _seed_pad_profile() -> None:
    """Write the player-0 pad profile once, if the file is not already there.

    Seeded rather than patched so a player's own remapping, which Cemu
    writes back to this same file, survives every later launch."""
    if PROFILE_PATH.exists():
        return
    try:
        PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
        PROFILE_PATH.write_text(_pad_profile_xml())
        log.info("seeded %s", PROFILE_PATH)
    except OSError as exc:
        log.warning("could not seed the pad profile at %s: %s", PROFILE_PATH, exc)


class Cemu(Emulator):
    name = "cemu"
    display_name = "Cemu"
    save_root = MLC_DIR
    save_subtrees = ("usr/save",)
    rom_extensions = ROM_EXTENSIONS
    log_path = CEMU_LOG_PATH
    # No SIGTERM handler: the default action ends the process at once, saves
    # are already on disk. The grace window only covers process-group teardown.
    term_timeout = float(os.environ.get("CEMU_STOP_WAIT", "5"))

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
        _patch_settings()
        _seed_pad_profile()
        if resume_slot:
            log.info(
                "cemu has no save states, resume_slot %s ignored "
                "(game resumes from its own save data)",
                resume_slot,
            )
        self._session_start = time.time()
        binary = os.environ.get("CEMU_BIN", "Cemu")
        cmd = [binary, "-f"]
        # Without an explicit override, Cemu's own default resolves to the
        # same mlc01 this broker dumps from.
        if os.environ.get("CEMU_MLC_DIR"):
            cmd += ["-m", str(MLC_DIR)]
        cmd += ["-g", str(rom_path)]
        log.info("launching cemu (rom=%s)", rom_path)
        self._spawn(cmd, base_launch_env())

    def _modified_title_saves(self) -> list[Path]:
        """Title save dirs holding a file written while the session ran."""
        selected = []
        if not SAVE_DIR.is_dir():
            return selected
        for high in sorted(SAVE_DIR.iterdir()):
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
        return selected

    def save_and_exit(self, slot: int) -> dict:
        self.stop()
        # The dump ships files newer than the session baseline. A save is a
        # directory tree the game rewrites only partially, so refresh every
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
