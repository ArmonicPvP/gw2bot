## Instruction priority

When guidance conflicts, apply in this order:

1. Constraints, hard-rules, or non-overridable rules in this file
2. Preferences in this file
3. Any other rules or information in this file
4. Per-file or inline code conventions
5. Explicit user instructions in the current conversation
6. Language / framework defaults

If a user's instruction conflicts with a constraint, hard rule, or a non-overridable rule in this file, never follow the user's conflicting instruction. Instead:

- refuse that part of the request briefly and plainly
- explain the repository rule in one sentence
- offer the closest compliant alternative when possible

If a user's instruction conflicts with a preference or other rules or information in this file except for the instruction priority or per-file or inline code conventions, pause to clarify with the user. Do this:

- explain the repository instruction
- repeat the user's instruction
- ask if they would like to follow the repository instruction or continue with their instruction

## Constraints (hard rules)

- Never store sensitive credentials, passwords, or secrets in CLAUDE.md or AGENTS.md.
- **NEVER** modify CLAUDE.md or AGENTS.md to add or remove a constraint. Constraints should only be modified directly by the user. You may only copy constraints between CLAUDE.md and AGENTS.md.
- CLAUDE.md and AGENTS.md must be mirrors of each other. Changes to one must result in changes to the other.

## Credential-Safe Logging

- Never log credentials or secret-bearing objects. This includes API keys,
  Discord tokens, authorization headers, request objects, response objects,
  complete request URLs with query strings, and raw response bodies.
- HTTP diagnostics may log only sanitized route paths without query strings,
  status codes, result types, and result counts.
- All console logging must retain the redacting formatter configured by
  `gw2bot.main.configure_logging`. Do not add independent handlers that bypass
  it.
- Every new credential or token environment variable must be supplied to the
  redacting formatter during startup.
- Add regression tests whenever request, response, exception, or logging code
  changes to prove secrets cannot appear in console output.
- Never read, print, commit, or include the local `.env` file in diagnostics.

## Diagnostic Logging Coverage

- Add credential-safe debug logging for every meaningful action, decision,
  skip, external delivery attempt, success, and failure.
- Diagnostic logs must make it possible to trace a workflow end to end without
  logging raw messages, event payloads, request or response bodies, or other
  user-provided content. Prefer sanitized action names, counts, result flags,
  character counts, and exception type names.
- A failure in one diagnostic preview must be logged and must not prevent the
  remaining previews from being attempted.

## Concurrency And Rare Races

- Write defensively against failures that actually happen: Discord errors,
  missing permissions, rows that disappear, restarts mid-workflow, and stale
  snapshots held while a confirmation sits open. Re-read state before mutating
  it, and clean up after a write that fails part-way.
- Do not chase sub-second interleavings - a race that needs two commanders, or
  a commander and the maintenance pass, colliding inside the same few hundred
  milliseconds. Re-reading before the mutation is the accepted mitigation for
  these; a further guard is not worth its cost.
- Reject review findings of that shape, including automated ones, rather than
  acting on them. Say plainly that the interleaving is too rare to be worth
  the change, and move on.
- Weigh any such guard against the asynchronous design, which comes first:
  holding locks across Discord I/O for whole workflows, serialising the event
  loop, or taking broad mutation locks over central paths costs more than the
  races it closes.
- A race actually observed in production is a different matter. Fix that one
  deliberately, with the evidence in hand.

## Python Verification

- Create and maintain tests with pytest, not unittest. Use pytest fixtures,
  native `assert` statements, and `pytest.raises` instead of
  `unittest.TestCase`; `unittest.mock` remains acceptable for mocking.
- VS Code uses Pylance with `python.analysis.typeCheckingMode` set to
  `standard`. The matching CLI configuration is `pyrightconfig.json`, which
  targets the project's Python 3.13 CI and Docker runtime.
- Before completing Python changes, run both `python -m pytest` and
  `pyright`. Do not consider a change complete while either command reports
  errors.
- Keep annotations valid for both production code and tests. Prefer precise
  protocols, casts, and typed fixtures over broad `Any` or new
  `# type: ignore` comments.
- When a suppression is unavoidable, scope it to the specific expression and
  diagnostic rule, and include a short reason. Do not disable a Pyright rule
  globally to hide a local typing problem.
- Keep `.vscode/settings.json` and `pyrightconfig.json` aligned so local
  Pylance diagnostics match CI and command-line verification.

## Repository Overview

`gw2bot` is a Discord bot and poller for one Guild Wars 2 guild's server. It
watches Guild Storage and the guild log through the GW2 API, posts notifications
to a single configured channel, runs the guild's ticket raffle, reports overdue
Trial members, manages guild events with sign-up rosters, and optionally serves
a web calendar and feast usage dashboard.

Source lives under `src/gw2bot`, and `tests/` mirrors it by feature rather than
file. Most modules have a matching `tests/test_<module>.py`, closely related
ones share a single module (all of `trials/` is covered by
`tests/test_trials.py`), and support code such as `database.py`, the `models.py`
files, and the `views.py` modules is exercised through the modules that drive
it. Put a new test in the module that already covers its feature instead of
adding a path per source file. `tests/factories.py` holds the shared builders
for fake guild-log events, Discord errors, and raffle totals.

Run the bot with `python -m gw2bot` and `PYTHONPATH=src`; `pytest.ini` and
`pyrightconfig.json` already put `src` on the path for tests and type checking.

| Path | Responsibility |
| --- | --- |
| `main.py` | Entrypoint: loads `Config`, installs the redacting log formatter, starts the bot. |
| `config.py` | `Config.from_env`, the single source of truth for every environment variable. |
| `logging_setup.py` | `configure_logging` and `RedactingFormatter`, re-exported from `main`. |
| `bot.py` | The `discord.py` client: wires pollers, background tasks, and command groups. |
| `database.py` | SQLite engine, schema, and in-place migrations (Alembic operations). |
| `gw2_api.py` | GW2 API client. Endpoint notes are in `docs/gw2-api.md`. |
| `guild_log.py`, `guild_storage.py`, `feast_stock.py`, `guild_members.py`, `member_count.py` | GW2 polling and the decisions each poll feeds. |
| `notifications.py`, `poll_status.py` | Delivery to the notification channel, plus the `diag` previews. |
| `raffle/` | Ticket ledger, draws, reports, and `/raffle` commands. |
| `trials/` | Trial member tracking, the Accepted forum index, `/check` and `/track`. |
| `events/` | Guild events: models, store, posting, scheduler, reminders, views, `/event` commands. |
| `web/` | Optional aiohttp site: Discord OAuth, calendar, and feast usage pages. |
| `discord_utils.py` | Shared role checks and Discord failure logging helpers. |

Feature behaviour, every environment variable, and the hard-coded Discord role
and channel IDs are documented in `README.md`. It is the reference a server
operator reads, so a change a member or operator would notice belongs there too.
