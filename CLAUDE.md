# CLAUDE.md

Project guidance for agents working in this repo lives in **@AGENTS.md** — read it first.

Repo-specific rules that are easy to regress (see AGENTS.md for the full list):

- **Gemini-only** — no `anthropic`/Claude dependency in the engine; the calling agent reasons.
- **Never a silent cut** — account for dropped/skipped/truncated data explicitly.
- **`docs/` is gitignored** — never commit planning/spec/dogfooding artifacts; ship clean code only.
- **Commit as the user; never add a Claude/AI co-author trailer.**
- **TDD, SDK-free tests** — `./venv/bin/python -m pytest -q`.

(Your global `~/.claude/CLAUDE.md` rules also apply.)
