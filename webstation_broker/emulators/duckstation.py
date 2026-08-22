"""DuckStation launcher (RomM platform "psx"): ROM folder→disc-image
resolution, settings.ini patching, and save state via graceful shutdown.

DuckStation has no runtime control channel; the whole lifecycle rides its
CLI and settings:

  resume: `-statefile <sav>` loads a state as part of boot. The broker
          resolves the state file itself because `-resume`/`-state` abort
          with an error dialog when the file is missing.
  save:   SIGTERM triggers a graceful shutdown which, with
          Main/SaveStateOnExit=true, writes `<serial>_resume.sav` into
          savestates/ before the process exits. The write is confirmed by
          diffing the directory across stop(). The resume state is the only
          state a shutdown produces, so it doubles as the broker's save
          state; the slot number is carried only for API symmetry.
"""

import logging
import os
import re
import signal
from pathlib import Path

from .base import Emulator, base_launch_env

log = logging.getLogger(__name__)

ROM_ROOT = Path(os.environ.get("ROM_ROOT", "/romm"))


def _default_data_dir() -> str:
    """DuckStation's Linux data root: $XDG_CONFIG_HOME/duckstation when set,
    otherwise ~/.local/share/duckstation."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg and os.path.isabs(xdg):
        return os.path.join(xdg, "duckstation")
    return os.path.join(os.environ.get("HOME", "/config"), ".local/share/duckstation")


DATA_DIR = Path(os.environ.get("DUCKSTATION_DATA_DIR", _default_data_dir()))
INI_PATH = DATA_DIR / "settings.ini"
SSTATE_DIR = DATA_DIR / "savestates"
DUCKSTATION_LOG_PATH = Path(
    os.environ.get("DUCKSTATION_LOG_PATH", "/config/duckstation.log")
)

# Disc formats duckstation-qt can boot, best first; a folder holding several
# candidates picks by this order so an .m3u playlist or .chd beats the raw
# .bin beside it.
ROM_EXTENSIONS = (
    ".m3u", ".chd", ".cue", ".pbp", ".ccd", ".mds",
    ".iso", ".img", ".ecm", ".bin", ".exe", ".psexe",
)
_ROM_SEARCH_GLOBS = ("*", "*/*")
_DISC_RE = re.compile(r"(?:^|[^a-z0-9])(?:disc|disk|cd)[\s._-]*(\d+)", re.IGNORECASE)


def _disc_number(rel: Path) -> int:
    match = _DISC_RE.search(str(rel))
    if match is None:
        return 1
    return max(1, int(match.group(1)))


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
        ranked.append(
            (_disc_number(rel), ROM_EXTENSIONS.index(ext), len(rel.parts), p.name.lower(), real)
        )
    if not ranked:
        return None
    return min(ranked)[4]


def _patch_ini() -> None:
    """Force broker-required settings.ini values before every launch.

    On a fresh container the file does not exist yet; seeding it with
    SetupWizardIncomplete already false keeps duckstation-qt from parking on
    the setup wizard, so the very first launch boots the disc."""
    patches: dict[tuple[str, str], str] = {
        ("Main", "SetupWizardIncomplete"): "SetupWizardIncomplete = false",
        # SIGTERM's graceful shutdown must not raise a confirm dialog, and
        # must write the resume state on the way out.
        ("Main", "ConfirmPowerOff"): "ConfirmPowerOff = false",
        ("Main", "SaveStateOnExit"): "SaveStateOnExit = true",
        # .bak copies would leak into the save archive dump.
        ("Main", "CreateSaveStateBackups"): "CreateSaveStateBackups = false",
        ("AutoUpdater", "CheckAtStartup"): "CheckAtStartup = false",
    }
    try:
        if not INI_PATH.exists():
            log.info("settings.ini not found at %s, seeding one", INI_PATH)
            INI_PATH.parent.mkdir(parents=True, exist_ok=True)
            INI_PATH.write_text("[Main]\nSetupWizardIncomplete = false\n")
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
            for (sec, key), val in patches.items():
                if section != sec:
                    continue
                if stripped.startswith(f"{key} =") or stripped.startswith(f"{key}="):
                    new_lines.append(val)
                    applied.add((sec, key))
                    matched = True
                    break
            if not matched:
                new_lines.append(line)
        missing = [(s, k, v) for (s, k), v in patches.items() if (s, k) not in applied]
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
        log.exception("settings.ini patch failed, broker settings NOT applied")


def _resume_snapshot() -> dict:
    if not SSTATE_DIR.is_dir():
        return {}
    snap = {}
    for p in SSTATE_DIR.glob("*_resume.sav"):
        try:
            st = p.stat()
            snap[p] = (st.st_size, st.st_mtime)
        except OSError:
            pass
    return snap


def _newest_resume_state() -> Path | None:
    """Newest `<serial>_resume.sav`; after a save restore the directory only
    holds this game's states, so newest-by-mtime is the session's state."""
    best: tuple[float, Path] | None = None
    for p, (_size, mtime) in _resume_snapshot().items():
        if best is None or mtime > best[0]:
            best = (mtime, p)
    return best[1] if best is not None else None


def _changed_resume_state(before: dict) -> Path | None:
    best: tuple[float, Path] | None = None
    for p, cur in _resume_snapshot().items():
        if before.get(p) != cur:
            mtime = cur[1]
            if best is None or mtime > best[0]:
                best = (mtime, p)
    return best[1] if best is not None else None


class Duckstation(Emulator):
    name = "duckstation"
    display_name = "DuckStation"
    save_root = DATA_DIR
    save_subtrees = ("memcards", "savestates")
    rom_extensions = ROM_EXTENSIONS
    log_path = DUCKSTATION_LOG_PATH
    # SIGTERM's graceful shutdown serializes the resume state before exiting;
    # give it room before the SIGKILL escalation discards it.
    term_timeout = float(os.environ.get("DUCKSTATION_STOP_WAIT", "30"))

    def clear_working_slot(self) -> None:
        """Drop every resume state left in SSTATE_DIR before a restore.

        Unlike RPCS3's per-title savestates/<serial>/ layout, DuckStation
        writes every title's <serial>_resume.sav into one flat directory, so a
        leftover from an earlier game or session in this container is
        otherwise indistinguishable from a state the archive is about to
        restore: _newest_resume_state() picks whichever file is newest with no
        serial filter at all. Anything still here belongs to a session that
        already exited, so dropping it all is what keeps a stale, unrelated
        resume state from being served as the new game's own; the same
        trade-off RPCS3's clear_working_slot makes."""
        if not SSTATE_DIR.is_dir():
            return
        for stale in SSTATE_DIR.glob("*_resume.sav"):
            try:
                stale.unlink()
                log.info("cleared stale resume state %s", stale.name)
            except OSError as exc:
                log.warning("could not clear stale resume state %s: %s", stale.name, exc)

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

        cmd = [os.environ.get("DUCKSTATION_BIN", "/opt/duckstation/AppRun"), "-batch", "-fullscreen"]
        state = _newest_resume_state() if resume_slot is not None else None
        if resume_slot is not None and state is None:
            log.warning("resume requested but no resume state in %s", SSTATE_DIR)
        if state is not None:
            cmd += ["-statefile", str(state)]
        cmd += ["--", str(rom_path)]

        log.info("launching duckstation (rom=%s, statefile=%s)", rom_path, state)
        self._spawn(cmd, base_launch_env())

    def save_and_exit(self, slot: int | None) -> dict:
        saved = False
        state_file = None
        was_alive = self.alive()
        before = _resume_snapshot()
        proc = self._proc
        self.stop()
        # SaveStateOnExit is forced true in _patch_ini, so DuckStation writes
        # its resume state on every graceful shutdown regardless of `slot`;
        # an exit with no state requested only skips reporting it here, the
        # file still lands in the save archive dump since it is newer than
        # the session baseline. We can only trust that write if SIGTERM was
        # enough: a SIGKILL escalation (term_timeout exceeded), or stop()
        # never confirming the process died at all, can cut the write off
        # mid-flight. A killed process's changed file is discarded outright
        # rather than just left unreported, since the archive dump sweeps up
        # anything with a fresh mtime whether or not this method reports it.
        killed = proc is None or proc.returncode is None or proc.returncode == -signal.SIGKILL
        if was_alive and slot is not None:
            p = _changed_resume_state(before)
            if killed:
                log.warning(
                    "duckstation had to be force-killed, discarding possibly-incomplete resume state"
                )
                if p is not None:
                    try:
                        p.unlink()
                    except OSError as exc:
                        log.warning("could not discard incomplete resume state %s: %s", p, exc)
            elif p is None:
                log.warning("no resume state written during shutdown")
            else:
                try:
                    st = p.stat()
                except OSError as exc:
                    log.warning("could not stat resume state %s: %s", p, exc)
                else:
                    saved = True
                    state_file = {"path": str(p), "size": st.st_size, "mtime": st.st_mtime}
        return {"state_saved": saved, "state_slot": slot, "state_file": state_file}
