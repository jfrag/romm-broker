"""Eden (Nintendo Switch) launcher: ROM resolution, qt-config.ini patching,
and SIGTERM-driven graceful shutdown.

Eden has no save states and no external control API. Persistence is the
game's own save data, which the emulated game commits directly to host files
under the virtual NAND (`nand/user/save/...`). Save paths are keyed by the
Switch profile UUID, so the profile store (`nand/system/save/8000000000000010`)
ships with the saves that way a save archive restored into a fresh
container brings its matching profile along and the paths line up.

Shutdown: Eden's Qt frontend routes SIGTERM through the event loop into a
normal window close (graceful emulation teardown). SIGINT is _exit(1) in
Eden never use it. The close path pops a confirmation dialog unless the
`confirmStop` UI setting is Ask_Never, so that is patched before every
launch.
"""

import logging
import os
import re
from pathlib import Path

from .base import Emulator, base_launch_env

log = logging.getLogger(__name__)

ROM_ROOT = Path(os.environ.get("ROM_ROOT", "/romm"))

CONFIG_DIR = Path(os.environ.get("EDEN_CONFIG_DIR", "/config/.config/eden"))
DATA_DIR = Path(os.environ.get("EDEN_DATA_DIR", "/config/.local/share/eden"))
INI_PATH = CONFIG_DIR / "qt-config.ini"
EDEN_LOG_PATH = Path(os.environ.get("EDEN_LOG_PATH", "/config/eden.log"))

# Formats Eden's loader boots directly, best first; a folder holding several
# candidates picks by this order.
ROM_EXTENSIONS = (".xci", ".nsp", ".nca", ".nro")
_ROM_SEARCH_GLOBS = ("*", "*/*")
# Updates/DLC sit beside base games in library folders; boot the base game.
_ADDON_RE = re.compile(r"(?:^|[^a-z0-9])(?:update|upd|dlc|patch)(?:[^a-z0-9]|$)", re.IGNORECASE)

# Eden serializes enum settings as their underlying integer;
# ConfirmStop::Ask_Never is 2. The `\default` flag must be false or the
# stored value is ignored in favor of the built-in default (Ask_Always),
# which pops a confirmation dialog on close and would hang a headless
# SIGTERM shutdown.
_INI_PATCHES: dict[tuple[str, str], str] = {
    ("UI", "confirmStop\\default"): "confirmStop\\default = false",
    ("UI", "confirmStop"): "confirmStop = 2",
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


def _patch_ini() -> None:
    """Force broker-required qt-config.ini settings before every launch."""
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
    except Exception:
        log.exception("qt-config.ini patch failed, broker settings NOT applied")


class Eden(Emulator):
    name = "eden"
    display_name = "Eden"
    save_root = DATA_DIR
    # Game saves plus the profile store: Switch save paths embed the profile
    # UUID, so the two must travel together for restored saves to resolve.
    save_subtrees = ("nand/user/save", "nand/system/save/8000000000000010")
    rom_extensions = ROM_EXTENSIONS
    log_path = EDEN_LOG_PATH
    # A running game takes longer than the base 5 s to tear down gracefully;
    # give SIGTERM room before escalating to SIGKILL.
    term_timeout = float(os.environ.get("EDEN_STOP_WAIT", "15"))

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
