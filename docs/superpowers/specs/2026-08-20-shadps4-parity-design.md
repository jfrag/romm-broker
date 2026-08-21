# shadPS4 parity pass

## Context

`webstation_broker/emulators/shadps4.py` is the thinnest emulator module in the broker
(169 lines vs. 449-529 for Dolphin/PCSX2). Investigation found that shadPS4's design is
sound: it correctly has no save states (`supports_states = False`, since PS4 games persist
their own save data as files, already handled by `save_subtrees` and the corrupted-marker
strip tested in `tests/test_saves.py`), no memory card, and no disc swap. Those are not gaps.

Two real gaps remain:

1. A path-validation inconsistency with its siblings (PCSX2, Dolphin) that lets a symlink
   inside a ROM folder escape `ROM_ROOT`.
2. No dedicated test file. Every other non-trivial emulator module (`pcsx2`, `dolphin`,
   `rpcs3`, `cemu`, `ppsspp`, `xemu`, `retroarch`) has one; shadps4 has none.

Explicitly out of scope (decided during brainstorming):

- **Boot-failure detection.** PCSX2 and RPCS3 run a `_boot_watchdog` thread against a
  PINE-style status socket to catch "process alive but stuck on an error dialog." shadPS4's
  IPC is stdin-only (no status readback), so there is no oracle to build this on. Most other
  emulators (Dolphin, PPSSPP, xemu, Cemu, etc.) also skip it for the same reason. Decision:
  leave shadps4 without it.
- **Live container verification.** `webstation-dev` (podman) is running with `/broker`
  bind-mounted live, but has no shadPS4 build downloaded and no PS4 ROM present. Getting a
  build requires the GUI version-manager flow. Decision: code + mocked tests only for this
  pass; live verification is a separate, later task.

## Change 1: fix the symlink escape in `resolve_rom_file`

`webstation_broker/api.py` resolves and validates the top-level ROM path against `ROM_ROOT`
before calling `resolve_rom_file()` (`Path(body.rom.path).resolve()` then
`is_relative_to(ROM_ROOT)`). That only guarantees the folder itself is inside the library; it
says nothing about symlinks *inside* that folder.

PCSX2's `_pick_rom_file` and Dolphin's `_pick_rom_file` both re-validate every candidate file
they consider: `real = p.resolve(); ... if not real.is_relative_to(ROM_ROOT): continue`.
Dolphin has a dedicated regression test for this
(`test_rom_pick_refuses_a_link_out_of_the_library`).

shadPS4's `resolve_rom_file` does not do this:

```python
def resolve_rom_file(self, path: Path) -> Path | None:
    if path.is_file():
        return path
    if not path.is_dir():
        return None
    eboot = path / "eboot.bin"
    if eboot.is_file():
        return eboot
    return path  # shadps4 appends eboot.bin to directory paths itself
```

If `eboot.bin` inside an otherwise-valid ROM folder is a symlink pointing outside `ROM_ROOT`,
`eboot.is_file()` follows it and returns `True`, and the unresolved symlink path is handed
straight to the shadPS4 binary as the boot target.

**Fix:** before returning `eboot`, resolve it and check it is still under `ROM_ROOT`, matching
the sibling emulators' guard. `path.is_file()` at the top of the function (the direct-file
case) is already the caller-resolved, caller-validated path, so it needs no additional check.

```python
def resolve_rom_file(self, path: Path) -> Path | None:
    if path.is_file():
        return path
    if not path.is_dir():
        return None
    eboot = path / "eboot.bin"
    try:
        if eboot.is_file() and eboot.resolve().is_relative_to(ROM_ROOT):
            return eboot
    except OSError:
        return None
    return path  # shadps4 appends eboot.bin to directory paths itself
```

No other behavior changes. This is the only production code change in this pass.

## Change 2: `tests/test_shadps4.py`

New file, following the structure and fixture patterns already established in
`tests/test_pcsx2.py` and `tests/test_dolphin.py` (module-global monkeypatching via
`tests/conftest.py`'s redirect pattern, no real subprocess/IPC).

Coverage:

**`_resolve_binary()`** (pure function over a tmp_path version tree)
- `SHADPS4_BIN` env override takes precedence over everything else
- a `Pre-release` folder's binary is preferred over any numbered release folder
- among numbered release folders, the highest semver wins (`v0.17.0` > `v0.9.9` > `v0.9.10`
  edge case to confirm numeric, not lexicographic, comparison)
- a release folder whose name doesn't parse as a version is skipped, not fatal
- `VERSIONS_DIR` missing entirely returns `None` (logs a warning, doesn't raise)
- a versions dir with no usable binary anywhere returns `None`

**`resolve_rom_file()`**
- a direct file path is returned as-is
- a folder containing `eboot.bin` returns that file
- a folder without `eboot.bin` returns the folder itself (shadPS4's own append-eboot.bin
  behavior)
- a folder containing an `eboot.bin` that is a symlink escaping `ROM_ROOT` is refused
  (regression test for Change 1)
- a path that is neither a file nor a directory returns `None`

**`launch()`**
- calls `stop()` first
- spawns with the resolved binary, `-f true -g <rom_path>` args, `stdin_pipe=True`, and
  `SHADPS4_ENABLE_IPC=true` in the environment
- sends `RUN` then `START` in that order over stdin
- `resume_slot` is not `None`: logged, ignored, no error, no state-related call attempted
  (there is nothing to load, shadPS4 resumes from its own save data)
- no usable binary: raises `RuntimeError` before spawning anything

**`stop()`**
- graceful path: writes `STOP\n`, flushes, process exits within `term_timeout` → forgets the
  process, no SIGTERM sent
- `BrokenPipeError` / `OSError` on the stdin write: falls through to the base class's
  SIGTERM/SIGKILL escalation
- `subprocess.TimeoutExpired` after `STOP`: falls through to the base class's escalation
- no process running: no-op, no exception

## Testing

`uv run pytest tests/test_shadps4.py -v` plus the existing `tests/test_emulators.py` and
`tests/test_saves.py` shadps4-related cases stay green. Full suite (`uv run pytest`) run once
at the end to confirm no regressions elsewhere (the `resolve_rom_file` change only touches
shadps4's own module).

## Non-goals (restated)

- No boot-failure watchdog for shadps4.
- No live/container end-to-end launch test.
- No changes to save-data handling, IPC protocol, or version-resolution semantics beyond the
  one symlink-validation fix.
