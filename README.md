# tsh — Timesheet Helper

A lightweight Windows desktop application for tracking time against Jira tickets in 15-minute blocks.

See `docs/superpowers/specs/2026-04-27-timesheet-helper-design.md` for the design spec
and `docs/plans/2026-04-27-timesheet-helper.md` for the implementation plan and progress.
For day-to-day commands, see `docs/dogfood-cheatsheet.md`.

## Development

This project uses [uv](https://docs.astral.sh/uv/) for dependency and venv management.
Install uv once if you don't have it:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Then from the project root:

```powershell
uv sync --all-extras       # install runtime + dev deps into .venv (creates it if missing)
uv run pytest -q           # run the full test suite
uv run tsh --help          # invoke the CLI
```

`uv run` activates the project venv automatically — no manual `.venv\Scripts\activate` needed.

### Common workflows

```powershell
uv add <package>           # add a runtime dependency (writes pyproject.toml + uv.lock + installs)
uv add --dev <package>     # add a dev-only dependency
uv remove <package>        # remove a dependency
uv lock                    # regenerate uv.lock after editing pyproject.toml by hand
```

The repo ships `pyproject.toml` and `uv.lock`. Both are checked in for reproducible installs.
