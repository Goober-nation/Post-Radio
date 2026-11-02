# Session 2 code review

Zero-trust review of `flow-integration`, 2026-11-02. Read-only: nothing was changed. The reviewer could not run
the tests (`troi` missing in its environment); the 203 tests pass in the project venv. "Confirmed" means the code
path was traced, not executed.

## Fix

1. **Data release install trusts the release** (`postradio/data.py:213-232`, same pattern in `check()`). Confirmed.
   - Manifest names are used as paths unvalidated; any `*.json` asset is copied into `data_dir` and can overwrite
     `data_release.json`, `build.json`, `genre_weights.json`.
   - A missing `sha256` is a bare `KeyError` mid-install.
   - The `.xz` store is unpacked with no size cap (xz bomb); the manifest's `bytes` is never checked.
   - Integrity is only the manifest in the same release; `browser_download_url`'s host is not checked.
   - Fix: allowlist exact names, require `Path(name).name == name`, cap the unpacked size, pin the host.
2. **ListenBrainz token leaks.**
   - Cache key (`postradio/http.py:114-116, 142`) ignores headers: a response fetched with one listener's token is
     cached up to 7 days and served to tokenless or other-token calls. Confirmed.
   - `Listener` (`postradio/api.py:84-100`) is a dataclass whose `repr` includes `lb_token`: `field(repr=False)`.
     Confirmed.
   - A FastAPI 422 can echo the request `input`, token included, back to the sender (`server.py:142-151`). Plausible.
   - No test asserts the token stays out of logs, cache rows and responses.
3. **Flow crashes on degenerate weights** (`postradio/flow.py`). Confirmed by reading.
   - `taste_profile` (85-87): total 0 when all weights are 0 → `ZeroDivisionError`.
   - `plan_path` (156, 165): all-zero scores, or `floor=0` with `taste_weight>0` → `ValueError` from `rng.choices`.
   - `fill` (263): `round(k*explore)` raises on NaN.
   - `validate()` (`config.py:312-339`) checks none of the `flow_*`, `floor` or rate-limit values.
   - `route_between` cost `1.05 - w` goes negative for weights > 1.05 (weights come from the release). Plausible.

## Document

4. **HTTP server has no auth and binds 0.0.0.0** (Dockerfile CMD, `postradio/server.py`). Confirmed.
   - Anyone reaching the port can run jobs, write feedback, save results, pick the LB user for `/deep-cuts`.
   - `Jobs.submit` has no limit; request `likes`/`plays` lists are unbounded.
   - The deadline is checked between stages only; `Http._reserve`/`_backoff` sleep uncancellably.
   - `docs/examples/compose.yaml` doesn't publish the port; the Dockerfile and docs don't warn.

## Lower

- `navidrome_url` may be `http://`; Subsonic MD5+salt auth sits in the query string (`feedback.py:73-74`). Plausible.
- Dockerfile: `python:3.13-slim` without digest, no lockfile or hashes (runs as uid 1000, has healthcheck: good).
- Responses have no size cap (`r.json()`, `Http.download` ignores `Content-Length`).
- `_safe` (`engine.py:247-258`) logs third-party exception text.
- Atlas page: `esc` doesn't escape `'`; lines 1237/1240 interpolate unescaped values (script constants today).
- Tracked build artefacts: `build/lib/**` and `postradio.egg-info/**` (checked: tracked). `config.yaml` is tracked
  (defaults only) though AGENTS.md says no config is committed.
- Weak assertion: `tests/test_providers.py:38` (`is not None`).
- Missing tests: token not leaked or shared via cache, hostile manifest names, zero/NaN weights in flow, server limits.

## Not a problem

- `lk.export-replay` is a placeholder; the git author email is the normal commit identity.
- `api.save` path traversal blocked by `slug()`; `import_dump` streams the tar and reads only `mbdump/artist`;
  Yandex URL regexes are anchored to Yandex hosts.
- No tokens, server addresses or personal paths in docs/, scripts/, tests/.
