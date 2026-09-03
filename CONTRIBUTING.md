# Contributing

Thanks for looking at `collections2mo2`. This file is the short version; see
[`docs/development.md`](docs/development.md) for the deeper guide and
[`docs/architecture.md`](docs/architecture.md) for module ownership and the non-obvious
facts verified against the live Nexus API.

## Dev setup

```
uv sync                       # Python >= 3.12
uv run pre-commit install     # one-time: installs the git pre-commit hook
```

## Running tests

```
uv run pytest -q
```

A few tests are marked `local` because they need a local tool
(`tools/7za.exe`, bootstrapped by `sevenzip.py` on first real run) that may not be
present in your environment or in CI - `pytest` runs them when the tool is there and
skips them otherwise.

End-to-end verification beyond the unit tests is running the full pipeline against the
development collection
[SKSE and Behaviours Essentials (h2uqa3)](https://www.nexusmods.com/games/skyrimspecialedition/collections/h2uqa3)
and spot-checking the resulting instance under `work/` (see `docs/development.md` for the
stage-by-stage commands).

## Lint and format

```
uvx ruff check src tests
uvx ruff format src tests
```

`.pre-commit-config.yaml` runs `ruff check --fix` and `ruff format` automatically on
commit, along with the standard `pre-commit-hooks` set and a local secret-check hook.

## Secrets

Never commit `.env` or a real Nexus API key. The pre-commit hook
(`scripts/check_secrets.py`) refuses commits that contain either, but treat that as a
backstop, not a substitute for checking `git status`/`git diff` yourself before pushing.

## Pull requests

Keep the README and `docs/` in sync with any behaviour you change - a PR that adds a
flag, command, or catalogue entry should update the relevant doc in the same change.
