# webstation-broker - Repository Guide for Contributors & Agents

Session broker and collaboration interface for the RomM webstation container. It launches emulators over REST, restores/archives save data, and runs the collab room (chat, webcam fanout, input routing) that the Selkies stream is embedded into.

---

## The stack at a glance

| Path                | Language              | Notes                                                    |
| -------------------- | --------------------- | --------------------------------------------------------- |
| `webstation_broker/` | Python 3.11+ (FastAPI) | pip-installable, console script `webstation-broker`       |
| `frontend/`          | Vanilla JS (Vite)      | room UI, served under the `SUBFOLDER` prefix              |
| `tests/`             | pytest                 | one test module per emulator/subsystem                    |
| `docs/`              | Markdown               | standalone-emulator and integration notes                 |

See [README.md](README.md) for the full request flow and save-archive layout.

---

## Skills - load before touching code

**`repo-conventions`** (`.claude/skills/repo-conventions/SKILL.md`) holds the house rules for this repo: test coverage, comment/docstring style and type hints, secrets, logging, and PR-issue linking. Invoke it before writing, editing, or reviewing any code, comments, tests, log statements, or PR/commit descriptions here. These rules are strict, not suggestions: follow them exactly, every time, even when not reminded.

---

## Repo-wide rules

**Branch off `master`; open PRs against `master`.** Don't push to `master` directly.
**Lint:** `ruff check webstation_broker` (CI runs this on every push/PR to `master`; see `.github/workflows/`).
**Tests travel with code.** New logic gets a test in `tests/`; new endpoints get endpoint tests.
**Don't commit until approved.** Never run `git commit` (or push) without the user explicitly signing off first.
**Link PRs to issues.** `Fixes #XXXX` for bug fixes, `Closes #XXXX` for feature implementations.

Full detail on comments, docstrings, logging, and secrets lives in `repo-conventions` - read it, don't duplicate it here.

---

## Quick command reference

```bash
pip install -e .                              # install (editable)
ruff check webstation_broker                  # lint
pytest                                         # run tests
pytest tests/test_flycast.py                   # run a subset
webstation-broker                              # run the app (console script)
```
