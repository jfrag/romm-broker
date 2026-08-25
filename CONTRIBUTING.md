# Contributing

Thanks for looking at webstation-broker. A few ground rules before you open a PR.

## Workflow

- Branch off `master`; open PRs against `master`. Don't push to `master` directly.
- Link the issue your PR addresses: `Fixes #XXXX` for a bug fix, `Closes #XXXX`
  for a feature, in the PR description.
- Keep commit messages short, concise, and accurate: what changed and why, no
  filler.

## Before you open a PR

```bash
uv venv && uv pip install -e . pytest "ruff==0.16.1"
.venv/bin/ruff check webstation_broker tests
.venv/bin/pytest -q
```

CI runs the same lint and test suite on every push and PR to `master`. There
is no frontend lint, test, or build step in CI; if you touch `frontend/`,
read your diff carefully before opening the PR.

## Code conventions

- **Tests travel with code.** New logic gets a test in `tests/`. New endpoints
  get endpoint tests.
- **Google-style docstrings and full type hints** on every module, class, and
  any function that isn't trivially self-describing. Type hints stay
  Python 3.9-safe: `Optional`/`Union` from `typing`, not `X | Y`.
- **No em-dashes** in code, comments, docstrings, log strings, or
  documentation. Use a comma, parentheses, a colon, or split into two
  sentences.
- **Comments explain why, not what.** Skip anything the code already says.
  No inline changelog of what a line used to do.
- **Log every error path**, with enough context to act on it (platform, rom,
  session id). Successful operations log at `info` or `debug`.
- **Never commit secrets.** No API keys, passwords, tokens, or broker secrets,
  hardcoded or otherwise. Configuration is env vars, read through
  `webstation_broker/settings.py`.

## Adding an emulator

There's a dedicated walkthrough for this:
[Adding an emulator](https://romm-streaming.github.io/romm-broker/docs/developer/adding-an-emulator).

## Documentation

Docs live under `docs/content/docs/` (Fumadocs) and deploy to GitHub Pages on
merge to `master`. If your change affects behavior a user or contributor would
read about, update the relevant page in the same PR.
