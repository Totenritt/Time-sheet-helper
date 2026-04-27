# tsh — Timesheet Helper

A lightweight Windows desktop application for tracking time against Jira tickets in 15-minute blocks.

See `docs/superpowers/specs/2026-04-27-timesheet-helper-design.md` for the design spec
and `docs/superpowers/plans/` (or `~/.claude/plans/crispy-zooming-charm.md`) for the implementation plan.

## Development

```bash
py -3.13 -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
pytest -q
```
