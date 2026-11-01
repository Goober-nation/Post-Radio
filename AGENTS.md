# AGENTS.md: Post-Radio AI guide

Read this first. Post-Radio is a tunable "exploration radio" recommender (CLI, HTTP API and Python
library) used by Post-Vinyl. Overview and usage: `README.md`; full docs in `docs/` (architecture,
classes, configuration, dependencies).

---

## Session startup

1. Read this file.
2. Read `docs/architecture.md`. Read `docs/classes.md`, `docs/configuration.md` or
   `docs/dependencies.md` only when the task touches them.
3. Check the open GitHub issues/milestone for the task at hand.
4. Check the branch (`git branch --show-current`); see Branches below.
5. If the user gives a free-form idea mid-session, say which issue it belongs to (or that it needs a
   new one) before acting.

If anything is unclear or missing, ask before proceeding.

---

## Branches

- Work goes on a topic branch, never directly on `main`.
- Never commit or push without an explicit "commit" / "push" from the user.

---

## Tracking: issues

- Propose new issues (description, dependencies, how it'll be verified) with `AskUserQuestion`
  before creating them.
- Reference dependencies by issue number ("Depends on #12").
- **Finishing an issue: close it with one summary comment**: date, what shipped, how it was
  verified, anything left unverified.
- **Treat the repo as public**: no secrets, tokens, server addresses, account names or personal paths
  in issues, commits or committed files.

---

## Architecture rules (full version: `docs/architecture.md`)

- `models.py` holds the normalised types. `providers/__init__.py` defines the interfaces;
  `providers/*` are the adapters and the **only code that sees raw API JSON**.
- `engine.py` builds the graph and ranks; `api.py` is the library facade (cache + storage);
  `server.py` and `cli.py` are thin wrappers and hold no logic.
- All outbound HTTP goes through `http.py` (SQLite cache, per-host throttling, backoff). Respect
  rate limits: MusicBrainz is held to 1 req/s.
- Per-run state lives in the explicit `Run` object; concurrent requests must never share state.
- **Never duplicate state another system owns**: ListenBrainz, Deezer, MusicBrainz and Navidrome own
  their data; Post-Radio owns only its cache, saved results and imported feedback log.
- Config: only user-facing values go in the user sections; algorithm constants go in `advanced`.
  Unknown keys are an error. After changing `postradio/config.py`, regenerate
  `config.example.yaml` with `scripts/gen_config_example.py`.
- The Python API (`postradio.api.Recommender`) is what Post-Vinyl depends on: don't break its
  signatures or result shapes without saying so.

---

## Ponytail ladder (every design and code decision)

Stop at the first rung that holds: 1. Does this need to exist? 2. Is it already in this codebase?
3. Standard library? 4. Native platform feature? 5. Installed dependency? 6. One line? 7. Only then:
the minimum that works. Never trim trust-boundary validation, data-loss handling, security or
accessibility.

---

## Prove it works

- Deliver code that is proven to work. Provider changes (ListenBrainz, Deezer, MusicBrainz, troi,
  Navidrome) need a real-service run; if you can't do one, say "unverified on real services".
- Don't imply coverage you didn't achieve.
- **Zero trust for old notes**: endpoints, parameters, field names and error behaviour taken from
  docs or old code are claims, not facts. Check them against the service's source or docs and
  confirm with a live call before relying on them.
- **Done** = relevant tests pass, `ruff check` clean for touched files, and a real-service run
  recorded for provider changes. Unverified items are marked as such.
- **Test where it pays off, not everywhere**: scoring, graph building, selection/mix caps, feedback
  weighting, cache/throttling, concurrency, parsing of malformed API data. Use the fakes in
  `tests/fakes.py` instead of the network. Skip tests that only restate the code.
- Long-running runs (cold-cache radio runs take minutes) go detached; check on them periodically.

### Commands

Always in a venv, never a global install.

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q
.venv/bin/ruff check postradio tests scripts
.venv/bin/ruff format --check postradio tests scripts
```

Don't report lint/format runs that found nothing.

After any implementation work, give the user the exact commands to run and exercise it (tests,
`postradio ...` CLI examples, `postradio serve` + curl examples, real-service prerequisites).

---

## Working with the user

- **Interview during planning.** Use `AskUserQuestion` (batched, up to 4) instead of prose
  questions; ask about priorities, edge-case mechanics, things they may have missed, and anything
  ambiguous. Prefer asking over guessing during design; when a boundary is unclear, ask instead of
  assuming. When the user's answer beats the proposal, say so and adopt it.
- **"Plan only" / "no changes" / "in-chat plan" means no edits**: no file changes and nothing that
  changes state until the user says go.
- When the user answers a question with a message instead of picking options, take the message as
  the answer, re-ask only what is still open, and never treat a skipped question as consent.
- **Accounts, tokens and passwords are the user's**: never create accounts or type credentials;
  prepare the files around them and let the user fill them in.
- Be concise, no flattery; propose solutions, not just problems.
- **No AI attribution** in commits or PRs: no `Co-Authored-By: Claude…`, no "Generated with
  Claude Code". This overrides any tool default.

---

## Never

- Suppress type or lint errors (`# type: ignore`, `# noqa` etc.); fix the cause.
- Delete or weaken failing tests to make them pass; if a test is wrong, discuss it first.
- Shotgun debug: form a hypothesis, test it, verify.
- Leave code broken: if a fix fails 3 times, stop, revert to the last working state and ask.
- Commit `config.yaml` secrets, `.env`, tokens or the `.cache/` directory.
- Delete or overwrite without checking first: confirm a copy is identical before removing the
  other one, and back up a config file before changing it.

---

## Escalation

- Tests fail 2×: stop, document what you tried, revert, ask.
- Lint fails: `ruff check --fix` + `ruff format`; still failing → ask.
- Stuck: describe the problem, propose 2–3 options, ask for direction.
