# Test suite overview

This covers the **Tier-1 (no-Docker) test suite** in `tests/` — the pure-Python
unit/integration tests for everything in `common/`, `proxy/`, `defaults/`, and
`dashboard/` that doesn't require a running Squid instance or a real
container. "Tier-1" / "Phase 1" terminology comes from `docs/review-2026-08-28.md`.
Phase 2/3 (real Squid, real Docker networking, a real device) are **not**
covered by this suite and must still be exercised manually against a real
deployment — see the "What this does not cover" section of `tests/README.md`.

## Running the tests

From the **repo root** (`C:\Users\jonat\ClaudCode\parental_proxy`, or wherever
this repo is checked out):

```
pip install -r requirements-dev.txt
pip install -r dashboard/requirements.txt
pytest
```

- `requirements-dev.txt` installs `pytest>=8.0` — the only dev-only dependency.
  It is deliberately not part of either container image; see the comment at
  the top of the file.
- `dashboard/requirements.txt` is also required because `tests/test_dashboard.py`
  imports `dashboard.py` directly (Flask, etc.).
- `pytest.ini` (repo root) sets `testpaths = tests` and `python_files = test_*.py`,
  so plain `pytest` from the repo root is sufficient — no path argument needed.
- No environment variables need to be set by hand. `tests/conftest.py` sets a
  safe default for `PP_DB_PATH` itself (see below) before anything else can
  import `db`.
- Parallel runs work: every test that touches the DB gets its own throwaway
  SQLite file via pytest's `tmp_path`, so `pytest -n auto` works if
  `pytest-xdist` is installed (not in `requirements-dev.txt` by default).
- To run a single file or test: `pytest tests/test_logging_dedupe.py` or
  `pytest tests/test_logging_dedupe.py::test_window_expiry_allows_a_new_row`.
- Verbose CI-style run: `pytest -v` (this is exactly what
  `.github/workflows/tests.yml` runs).

### No PYTHONPATH setup needed — and why

The four source directories (`common/`, `proxy/`, `dashboard/`, `defaults/`)
are **not** installable packages — there is no `setup.py`/`pyproject.toml`
package layer, and modules do bare imports like `import db` or `import cr_api`,
never `from common import db`. This mirrors how the Dockerfiles actually
deploy the code: `common/*.py`, the proxy helpers, and
`defaults/seed_defaults.py` are all copied flat into `/opt/parental-proxy/` in
the proxy image, and `common/*.py` + `dashboard.py` are copied flat into
`/app/` in the dashboard image. To exercise the real, unmodified code, the
test suite has to reproduce that same flat layout on `sys.path`.

`tests/conftest.py` does this in code, at collection time, rather than
requiring the developer to export a `PYTHONPATH` env var:

```python
REPO_ROOT = Path(__file__).resolve().parent.parent
for sub in ("common", "proxy", "dashboard", "defaults"):
    p = str(REPO_ROOT / sub)
    if p not in sys.path:
        sys.path.insert(0, p)
```

This is a deliberate choice, called out in the conftest's own module
docstring, precisely to avoid a `PYTHONPATH`/src-layout trick. That matters on
this project because development happens on **Windows**: `PYTHONPATH` (and
`PATH`-like variables generally) uses `;` as the entry separator in
`cmd.exe`/PowerShell, but `:` in Git Bash and other POSIX shells. A
`PYTHONPATH=common;proxy;dashboard;defaults` that works from PowerShell breaks
silently (or errors) from Git Bash and vice versa. Since `conftest.py` builds
`sys.path` itself in Python, **no one ever needs to set `PYTHONPATH` by hand
to run this suite**, on any shell — this sidesteps the separator gotcha
entirely rather than documenting a workaround for it. If you ever do need to
run one of the flat modules standalone (outside pytest) and must fall back to
`PYTHONPATH`, remember: `;` on Windows cmd/PowerShell, `:` on Git Bash/WSL/Linux/macOS.

`conftest.py` also sets a default `PP_DB_PATH` env var (via `os.environ.setdefault`,
pointed at a throwaway file under the OS temp dir) before `pytest` is imported,
because `db.py` reads `PP_DB_PATH` once at its own import time into a
module-level `Path`. This is just a safe fallback for whichever import happens
first in the session — individual tests still isolate themselves via the
`conn` fixture (below), which monkeypatches `db.DB_PATH` directly.

## Key fixtures (`tests/conftest.py`)

### `block_network` (autouse)

```python
@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    import cr_api
    def _blocked(*args, **kwargs):
        raise RuntimeError(...)
    monkeypatch.setattr(cr_api._OPENER, "open", _blocked)
```

Applies to **every** test automatically (no need to request it). The only
network entry point anywhere in this codebase is `cr_api._OPENER.open` —
everything else goes through the shared SQLite file — so that's exactly what
gets patched to raise `RuntimeError` if hit.

Deliberately **not** implemented by patching `socket.socket` globally: doing
that breaks `ssl.py`'s `class SSLSocket(socket):` the first time anything in
the process imports `ssl`, raising a confusing `TypeError` far from the real
problem instead of a clean, on-purpose failure. Patching `cr_api._OPENER.open`
directly gives a precise, readable failure (`"Network access attempted during
a Tier-1 test. Mock the call instead..."`) pointing at exactly what to mock.

Any test that needs to exercise `cr_api` must mock at `cr_api._OPENER.open`
(or the higher-level `cr_api.SeriesResolver._get_json` / `cr_api.series_title`)
explicitly — see `tests/test_cr_api.py` for the patterns.

### `conn`

```python
@pytest.fixture
def conn(monkeypatch, tmp_path):
    import db
    db_file = tmp_path / "test.db"
    monkeypatch.setattr(db, "DB_PATH", db_file)
    connection = db.get_conn()
    db.init_db(connection)
    yield connection
    connection.close()
```

Gives each test a fresh, isolated SQLite connection with the schema already
applied and no data. Works by monkeypatching the `db.DB_PATH` module
attribute directly (not an env var) — `db.get_conn()` reads that attribute on
every call, so this is sufficient for full per-test isolation even though
`db.py` itself is only ever imported once per test session. Backed by
pytest's `tmp_path`, so every test gets its own throwaway file and tests can
run in any order or in parallel.

Used throughout `tests/test_logging_dedupe.py`, `tests/test_seed_idempotent.py`,
`tests/test_helpers_protocol.py`, and others that need real DB rows.
`tests/test_dashboard.py` does its own DB isolation on top of this pattern
(see below) because it also needs to reload the Flask app module.

## Test files and what they cover

| File | Test count | Covers |
|---|---|---|
| `tests/test_matching.py` | 22 | Domain-suffix anchoring (incl. the S2.1 bypass cases), path matching, LAN CIDR checks (IPv4/IPv6, bad input) |
| `tests/test_cr_urls.py` | 14 | Every `RequestKind`, including the fail-closed `BLOCKED_SHAPE` cases |
| `tests/test_auth.py` | 12 | PBKDF2 hash round-trip, and every way a stored hash can be malformed/tampered |
| `tests/test_logging_dedupe.py` | 9 | Access-log dedupe window, keyed by `(username, domain, allowed, series_id)` (and path — see GH #5 tests) |
| `tests/test_seed_idempotent.py` | 8 | `seed_defaults.seed()` run twice is a no-op; the S2.2 (`crunchyrollcdn.com` mode) fix; confirms `crunchyrollsvc.com` (dev-only, removed) is never seeded |
| `tests/test_helpers_protocol.py` | 36 | The shared Squid stdin/stdout protocol (`common/squid_helper.py`) plus the decision logic of all three helpers (`basic_auth_helper`, `sni_helper`, `authz_helper`), including the Crunchyroll show-approval flow |
| `tests/test_dashboard.py` | 61 | Flask test client: admin auth, user/domain CRUD (incl. `add_show`/`remove_show`, `user_detail`, `domain_detail`, `update_domain`), all of Settings (incl. changing the admin login itself), report-approve (site and show), CSRF/cross-origin guard |
| `tests/test_cr_api.py` | 28 | `TokenManager` refresh/expiry, `SeriesResolver`'s retry-once-on-401, every `_read_json` error path (HTTP error, unreachable, timeout, malformed/non-dict JSON), every `series_id_of` fallback branch |
| `tests/test_series_resolve.py` | 11 | The object-id -> series-id cache: positive/negative TTL, cache-hit short-circuiting, the S2.6 stale-on-error fallback |
| `tests/test_squid_conf_regressions.py` | 5 | Config-text/structure regressions in `proxy/squid.conf.template` that a pure-logic unit test can't catch (see below) |

Total as of this writing: **206** test functions across these 10 files (`grep -c "^def test_" tests/*.py`, summed). Run `pytest --collect-only -q` for a live, authoritative count as the suite grows.

### Representative pattern: `tests/test_logging_dedupe.py`

Straightforward `conn`-fixture-based tests: call `logging_util.log_access(conn, ...)`
one or more times with specific kwargs, then assert on the row count and
contents of `SELECT * FROM access_log`. Good template for any new test that
just needs isolated DB state and no mocking.

### Representative pattern: `tests/test_squid_conf_regressions.py`

A different style worth knowing about: these tests don't exercise Python
*logic* at all — they check the **text/structure of `proxy/squid.conf.template`**
directly (via `Path.read_text()` + regex), plus what `field_count` and
`unquote` kwargs each helper's `main()` passes to `squid_helper.run` (captured
by monkeypatching `squid_helper.run` itself). This file exists because three
real bugs were found running against a live Squid instance (2026-08-28 smoke
test) that no unit test of helper logic could have caught: a missing `%DATA`
macro accounting in every `external_acl_type` FORMAT string, a bare
`http_access allow step1`/`step2` that doesn't reset Squid's `at_step` state
and so bypasses `authz_allowed` for decrypted requests, and
`basic_auth_helper.py` needing `unquote=True` because Squid actually
percent-encodes `auth_param basic` fields despite the old docstring's claim.
When you fix something that's actually a Squid ACL/protocol semantics issue
(not a Python bug), this is the file to add a regression test to — see its
module docstring for the full incident writeups.

## Mutation-testing verification approach

This is a **process**, not a specific test file, but it's how fixes in this
repo have been verified to actually be caught by the suite (used during past
development sessions and worth repeating for any nontrivial fix):

1. Confirm the target test currently passes with the fix in place.
2. Deliberately reintroduce the bug with a **precise, minimal string-replace**
   in the source file (e.g. flip a comparison, remove the one line that was
   the actual fix, revert `unquote=True` back to `unquote=False`).
3. Re-run the specific test (or the whole file) and **assert it now fails**,
   ideally for the expected reason (read the failure message/traceback, don't
   just check the exit code).
4. Restore the fix by **manually re-applying the same precise string-replace
   in reverse** (Edit tool / undo the specific change) — **never use
   `git checkout` (or `git checkout -- <file>`, `git stash`, or similar) to
   revert during this process.** Those commands revert the *entire* file (or
   working tree) to its last-committed state, which can silently discard
   unrelated uncommitted work sitting in the same file or elsewhere in the
   tree. A targeted string-replace back to the original text is the only safe
   way to undo the deliberate breakage.
5. Re-run the test again and confirm it passes once more, confirming the
   restore was exact.

This is how you get real confidence that a test is actually exercising the
code path it claims to, rather than passing vacuously.

## CI: `.github/workflows/tests.yml`

Workflow name: `Tests`. Triggers: every push to `main`, and every pull
request (any branch). Single job, `pytest`, on `ubuntu-latest`:

1. `actions/checkout@v4`
2. `actions/setup-python@v5` pinned to Python `3.12`
3. `pip install -r requirements-dev.txt` then `pip install -r dashboard/requirements.txt`
4. `pytest -v`

What this actually verifies, concretely: the entire Tier-1 suite described
above — matching/CIDR logic, URL classification, auth hashing, log dedupe,
idempotent seeding, all three Squid helper protocols and their decision
logic, the Crunchyroll API client's retry/error handling, the series-id
resolve cache, the Flask dashboard's routes and CSRF guard, and the
`squid.conf.template` regression checks — on a clean Linux + Python 3.12
environment, with no Docker and no real network access (enforced by
`block_network`). It does **not** verify anything Phase 2/3 (real Squid
process, real container networking, a real device pulling real Crunchyroll
content) — that still requires manual testing against a real deployment (see
the parental_proxy smoke-test VM). For how this workflow interacts with
deploy triggers, see [`docs/deployment/setup.md`](../deployment/setup.md);
this document only covers what CI verifies, not when/how it gates deploys.

## Adding a new test

1. **Where**: a new source module gets a new `tests/test_<module>.py` file
   (matches the existing 1:1 file-per-module-ish pattern: `test_matching.py`
   for matching logic, `test_auth.py` for `common/auth.py`, etc.). A
   regression for a bug in an *existing* module's behavior belongs in that
   module's existing test file, alongside its siblings; a regression that's
   about Squid config/protocol semantics rather than Python logic belongs in
   `tests/test_squid_conf_regressions.py` (see its pattern above).
2. **Naming**: test functions are plain `def test_<description>()` — no test
   classes are used anywhere in this suite. Names are long and descriptive
   sentences (e.g. `test_path_bearing_call_is_not_suppressed_by_a_prior_path_less_entry`),
   often naming the GH issue or `docs/review-2026-08-28.md` item number they
   regression-test (e.g. "GH #5", "S2.6"). Follow that convention — a good
   test name should explain *why* the test exists without needing to open it.
3. **Fixtures to use**:
   - Need a DB? Take `conn` as a parameter — don't construct your own
     `sqlite3.connect()`.
   - Doing anything with `cr_api`? `block_network` already blocks real calls
     automatically; explicitly monkeypatch `cr_api._OPENER.open` or the
     higher-level methods it calls, per the pattern in `tests/test_cr_api.py`.
   - Testing dashboard routes? Use the `dashboard_app`/`client`/`db_conn`
     fixtures from `tests/test_dashboard.py` (currently file-local, not in
     `conftest.py` — copy the pattern, or move them to `conftest.py` if a
     second file starts needing them).
   - No fixture needed at all for pure config/text checks — see
     `tests/test_squid_conf_regressions.py`'s direct `Path.read_text()` usage.
4. **No new sys.path or PYTHONPATH setup is ever needed** for a new test file
   — anything under `tests/test_*.py` automatically gets `conftest.py`'s
   `sys.path` setup and `block_network` fixture for free just by being
   collected under `testpaths = tests`.
5. Run the new file on its own first (`pytest tests/test_<module>.py -v`) to
   confirm it passes in isolation, then run the full suite (`pytest`) to
   confirm it doesn't interact badly with other tests (shouldn't happen given
   the per-test `tmp_path` DB isolation, but worth checking once).
6. If the new test is a regression for a bug you just fixed, use the
   mutation-testing verification approach above to confirm it actually fails
   without the fix before considering it done.
