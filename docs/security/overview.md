# Security Overview

This document describes the security model of parental_proxy as it actually
exists in code today: how each credential is checked, what trust boundary
each component sits behind, and where there are no protections at all. It is
written for an AI agent (or human) about to extend this codebase — read it
before touching auth, the Squid helper protocol, or the SSL-bump chain.

No secret values (passwords, hashes, salts, keys) appear in this file, only
mechanisms and file/function references.

---

## 1. Dashboard admin authentication

**Where:** `common/auth.py` (hashing), `dashboard/dashboard.py` (HTTP Basic
challenge, credential storage, bootstrap).

### Password hashing

`common/auth.py` implements PBKDF2-HMAC-SHA256, stdlib-only (no third-party
crypto dependency, since the proxy container needs to verify passwords too
and intentionally has no pip packages installed):

- `hash_password(password: str) -> str` — generates a 16-byte random salt via
  `os.urandom(16)`, runs `hashlib.pbkdf2_hmac("sha256", ..., iterations=260_000)`,
  and returns an encoded string of the form
  `pbkdf2_sha256$<iterations>$<salt_hex>$<digest_hex>` (the `$`-delimited
  format is self-describing, similar to Django's/passlib's convention, so the
  iteration count travels with the hash and can be bumped later without
  invalidating stored hashes).
- `verify_password(password: str, encoded: str) -> bool` — splits the encoded
  string, re-runs PBKDF2 with the embedded algorithm/iteration/salt, and
  compares digests with `hmac.compare_digest` (constant-time, avoids a
  timing side-channel on hash comparison).
- Constants: `ITERATIONS = 260_000`, `ALGORITHM = "pbkdf2_sha256"`.

This same module is used for **both** the dashboard admin password and every
per-person proxy login password (see §3) — one hashing scheme, two different
credential stores (`settings.admin_password_hash` vs. `users.password_hash`).

### Where admin credentials live and how they're checked

All in `dashboard/dashboard.py`:

- `bootstrap_admin()` — runs once at import time (called unconditionally near
  the bottom of the file, before `waitress.serve`). If `settings.admin_username`
  isn't already set in the DB, seeds it from `DASHBOARD_USER` env var (default
  `"admin"`). If `settings.admin_password_hash` isn't already set, seeds it
  from `DASHBOARD_PASSWORD` env var if provided, otherwise generates a random
  password via `secrets.token_urlsafe(12)`, hashes it, and prints it once to
  stderr (`docker compose logs dashboard`) since there is no other way to
  recover it. After first run, env vars are never consulted again — the
  admin login is fully DB-resident and editable from the Settings page
  (`update_admin()` at `/settings/admin`).
- `_check_admin_auth(basic_auth) -> bool` — reads `admin_username` and
  `admin_password_hash` from the `settings` table, and returns
  `basic_auth.username == expected_user and auth.verify_password(basic_auth.password, expected_hash)`.
  Note this is a plain `==` on username (not constant-time) but a
  constant-time compare on the password digest via `verify_password`.
- `require_admin(view)` — a decorator (`functools.wraps`) that calls
  `_check_admin_auth(request.authorization)` and returns a 401 with a
  `WWW-Authenticate: Basic realm="Parental Proxy Admin"` header on failure.
  Applied to essentially every route in the dashboard except `/ca-cert`
  (deliberately public — the CA certificate is not a secret, every client
  device needs to fetch it) and the `/blocked` friendly block page.

### Session mechanism

There is **no server-side session** and no session cookie for the admin
login. Every request is independently authenticated via the `Authorization:
Basic ...` header, which the browser caches and resends automatically for
the lifetime of the browser session (standard HTTP Basic behavior — this is
what "logged in" means here, not a cookie or token issued by the app).
`app.secret_key` is set (`db.get_setting(conn, "secret_key")`, seeded once by
`bootstrap_admin()` via `secrets.token_hex(32)`) but at the time of writing
nothing in `dashboard.py` calls `flask.session` — the key exists but is
unused for auth; if Flask sessions are added later, this key is already
provisioned correctly (random, DB-persisted, not re-generated per process).

### Per-person proxy login passwords (same hashing, different store)

`add_user()` (`/users/add`) and `reset_password()` (`/users/reset-password`)
in `dashboard/dashboard.py` both call `auth.hash_password(password)` and
store the result in `users.password_hash`. These are the credentials each
family member configures in their device's proxy settings — entirely
separate from the dashboard admin login. See §3 for how Squid verifies them.

---

## 2. Dashboard CSRF protection

**Where:** `dashboard/dashboard.py`, `_reject_cross_origin_writes()`
(registered via `@app.before_request`).

Because the dashboard authenticates with HTTP Basic rather than a session
cookie plus a CSRF token, a classic CSRF-token-in-form defense isn't a
natural fit — but the ambient-credential problem is the same as cookies:
once a browser has entered Basic credentials for this origin, it will
attach them automatically to *any* request to that origin, including one
triggered by a malicious cross-site page (e.g. an auto-submitting hidden
form POSTing to `/users/delete`).

The mitigation implemented is an **Origin/Referer allowlist check**:

```python
@app.before_request
def _reject_cross_origin_writes():
    if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return None
    for header in ("Origin", "Referer"):
        value = request.headers.get(header)
        if value:
            if urlparse(value).netloc != request.host:
                return Response("Cross-origin request blocked.", 403)
            return None
    return None
```

Logic: only state-changing methods are checked (GET/HEAD are exempt, matching
the assumption that no GET route mutates state). For each candidate header
in order, if present and its host (`urlparse(value).netloc`) doesn't match
`request.host`, the request is rejected with 403. If *neither* header is
present, the request is allowed through — the reasoning documented inline is
that a real browser CSRF attack always carries at least one of these headers
on a cross-site POST, so the only traffic with neither header is a
non-browser client (curl, a script, an API caller) that has no ambient
credentials to steal in the first place — it must have supplied Basic auth
itself.

**Limitations to know before relying on or extending this:**

- It is a same-origin check, not a cryptographic anti-CSRF token — it trusts
  that browsers reliably send `Origin` (they do, for cross-site
  state-changing requests, per the Fetch spec) and that no legitimate
  same-site flow needs to omit both headers.
- It only checks `netloc` (host:port), not scheme — an attacker on
  `http://dashboardhost` vs. the real `https://dashboardhost` would pass,
  but this deployment has no TLS on the dashboard at all (see §7), so that
  distinction doesn't currently apply.
- It is global (`before_request` on the whole app), not scoped per-route —
  if a future route needs a legitimate cross-origin POST (e.g. a webhook),
  it will need an explicit carve-out here.
- It does not protect GET routes that have side effects. If a future route
  is added that mutates state on GET (it shouldn't be, but nothing enforces
  that), this check does not cover it.

---

## 3. Squid-side per-user authentication (HTTP Basic via `basic_auth_helper.py`)

**Where:** `proxy/basic_auth_helper.py`, driven by `common/squid_helper.py`;
configured in `proxy/squid.conf.template` under `auth_param basic program`.

Each family member gets one login (`auth_param basic realm "Parental Proxy"`,
`credentialsttl 4 hours` — Squid caches a successful check for 4 hours before
re-asking the helper). Squid calls the external helper process
(`basic_auth_helper.py`) over stdin/stdout — see §4 for the general helper
protocol — passing `username password` fields.

### The percent-encoding gotcha

The docstring at the top of `proxy/basic_auth_helper.py` documents a
correction made against a real Squid instance:

> Confirmed against a real Squid 5.7 instance (2026-08-28): despite the
> "classic Basic doesn't percent-encode" folklore this docstring used to
> repeat, this Squid version *does* percent-encode both fields exactly like
> its external_acl_type helpers — a raw capture showed a password of
> `a b%c d` arriving as `a%20b%25c%20d`.

In other words: the common assumption that Squid's `auth_param basic`
protocol passes the raw username/password unescaped (as "classic" HTTP Basic
auth helpers historically did) is **wrong for this Squid version**. Squid
percent-encodes both fields the same way it encodes fields for
`external_acl_type` helpers (see §4). Before this was discovered, the helper
was configured with `unquote=False`, meaning any password containing a
space, `%`, or another character requiring escaping could never successfully
authenticate — Squid would send the encoded form, the helper would compare
it verbatim against the (unencoded) stored password, and it would never
match.

### How the helper addresses it

`basic_auth_helper.py`'s `main()` calls the shared loop with:

```python
squid_helper.run("basic_auth_helper", 2, check, unquote=True, keep_trailing_spaces=True)
```

- `unquote=True` — `common/squid_helper.py`'s `run()` applies
  `urllib.parse.unquote(p)` to every field before calling the handler,
  undoing Squid's percent-encoding.
- `keep_trailing_spaces=True` — the line is split with
  `line.split(" ", field_count - 1)` (i.e. `split(" ", 1)` for 2 fields)
  rather than `line.split()`, so only the *first* space separates username
  from password — everything after it, including further literal spaces, is
  preserved as part of the password field. This matters because a proxy
  password may legitimately contain spaces.
- `check(conn, username, password)` looks up `users.password_hash` by
  username and calls `auth.verify_password(password, row["password_hash"])`
  (same PBKDF2 verification as the dashboard admin login, §1). A missing
  username returns `False` without a timing-safe comparison against a dummy
  hash — a minor username-enumeration-via-timing surface, not mitigated.

**Anyone extending or reusing this Basic-auth-over-Squid pattern should not
assume "no encoding" for either helper type — always percent-decode fields
read from Squid**, and confirm against the actual Squid version in use if
the behavior is ever in doubt (this project's finding was version-specific
folklore-correction, not a documented Squid guarantee found in advance).

---

## 4. The `external_acl_type` helper protocol — internal trust boundary

**Where:** `common/squid_helper.py` (shared loop), `proxy/sni_helper.py`,
`proxy/authz_helper.py`; wired up in `proxy/squid.conf.template` via
`external_acl_type ... /usr/bin/python3 /opt/parental-proxy/<helper>.py`.

### Protocol mechanics

Squid speaks a simple line protocol to each helper, implemented once in
`common/squid_helper.py`'s `run(name, field_count, handler, *, unquote=True,
keep_trailing_spaces=False)`:

- Squid launches the helper as a **long-lived local subprocess** (per
  `children-max=N` in the `external_acl_type` line) and writes one
  space-separated, percent-encoded line per request to its stdin.
- The helper reads `sys.stdin` in a loop, splits each line into exactly
  `field_count` fields, percent-decodes them (`urllib.parse.unquote`, unless
  `unquote=False`), and calls `handler(conn, *fields)`.
- The handler's boolean return is written back as `"OK\n"` or `"ERR\n"` to
  stdout, flushed immediately (`sys.stdout.flush()`), so Squid can read the
  reply before sending the next line.
- Any exception raised by the handler is caught at the loop level, logged to
  stderr, and treated as `False`/`"ERR\n"` — **one malformed or
  exception-raising request must never kill the helper process** (a crashed
  helper would leave Squid unable to evaluate that ACL at all for every
  subsequent request until Squid restarts it).
- A line with the wrong number of fields (e.g. the field-count-off-by-one
  bug documented in `squid.conf.template` around the trailing `%DATA`
  macro) is answered `"ERR\n"` without ever calling the handler.

`sni_helper.py` uses `field_count=4` (`%LOGIN %>a %ssl::>sni %DATA`) and is
invoked four times under four different `sys.argv[1]` modes (`bump`,
`trusted`, `splice`, `block_page`) dispatched via a `HANDLERS` dict — each
mode backs a separate `acl ... external ...` line and a separate long-lived
helper process (no shared in-memory state between the four modes).
`authz_helper.py` uses `field_count=5` (`%LOGIN %>a %DST %PATH %DATA`) and is
invoked once, only for domains already decided to be in `bump` mode.

### Threat model: local trust, not network-exposed

This is the key property to reason about when extending these helpers:
**Squid invokes them as local subprocesses communicating over stdin/stdout
pipes, never over a network socket.** There is no listening port, no
network-reachable attack surface, and no way for an external client to talk
to `sni_helper.py` or `authz_helper.py` directly — only Squid's own C code,
running as the same container's process tree, can write to their stdin.

Consequences for what threat model applies:

- **Does apply:** input to these helpers must be treated as coming from a
  trusted-but-fallible local component (Squid itself) that can send
  malformed, unexpected, or (per §3's discovery) unexpectedly-encoded data
  due to version quirks or config mistakes — hence the defensive per-line
  try/except and field-count validation in `squid_helper.run()`. The values
  inside the fields (`%LOGIN`, `%DST`, SNI, path) originate from the
  end-user's traffic and **are** attacker-influenceable in content (a
  malicious hostname/path/SNI string), so the handlers must not trust those
  values as safe — e.g. all DB access goes through parameterized queries
  (`conn.execute("... WHERE username = ?", (username,))`), and hostname/path
  parsing (`_split_host_port`, `matching.find_domain`) treats them as
  arbitrary untrusted strings, not code.
- **Does not apply:** there is no need for these helpers to authenticate
  their caller, rate-limit requests, defend against a network-based
  attacker connecting directly to them, or worry about transport
  confidentiality between "client" and "server" — there is no network hop
  between Squid and the helper to secure. Do not add TCP listeners, HTTP
  endpoints, or auth tokens to these helpers on the assumption they need
  network-facing hardening; that would be solving a problem this design
  doesn't have, at the cost of introducing a new one (an actually
  network-reachable surface).

If a future change ever makes any of these helpers reachable other than via
Squid's own subprocess stdin (e.g. exposing one as a standalone service for
reuse), the threat model changes completely and this section's guidance no
longer applies — that helper would need the same scrutiny as the dashboard
itself (auth, input validation against a hostile network peer, etc.).

---

## 5. SSL-Bump / CA certificate trust model

**Where:** CA generation in `proxy/entrypoint.sh`; bump-chain configuration
and mode semantics in `proxy/squid.conf.template`; per-domain mode decisions
in `proxy/sni_helper.py`; public cert download at the dashboard's `/ca-cert`
route (`dashboard/dashboard.py`, deliberately unauthenticated since a
certificate is not a secret).

### What the CA can do

`entrypoint.sh` generates a self-signed root CA on first run if
`$SSL_DIR/ca_cert.pem` / `ca_key.pem` don't already exist:

```sh
openssl req -new -newkey rsa:2048 -sha256 -days 3650 -nodes -x509 \
  -keyout "$SSL_DIR/ca_key.pem" -out "$SSL_DIR/ca_cert.pem" \
  -subj "/O=${CA_ORG:-Parental Proxy}/CN=${CA_COMMON_NAME:-Parental Proxy CA}" \
  -addext "basicConstraints=critical,CA:TRUE" \
  -addext "keyUsage=critical,keyCertSign,cRLSign"
```

`basicConstraints=CA:TRUE` and `keyUsage=keyCertSign` are explicitly forced
(not just relying on the base image's `openssl.cnf` defaults) so Squid's
`sslcrtd_program` (`security_file_certgen`, configured in
`squid.conf.template`'s `sslcrtd_program`/`sslcrtd_children`) can mint
per-site leaf certificates signed by this key on the fly, as directed by
`http_port ... ssl-bump generate-host-certificates=on cert=... key=...`.

**Once a device is told to trust this CA certificate as a root, that CA can
mint a certificate for *any* hostname and have the device accept it as
genuine** — this is not a limitation specific to this project, it is the
fundamental mechanism of how TLS-terminating ("MITM") proxies work: the
proxy presents a certificate for the real destination hostname, signed by a
key the client trusts, and the client has no way to distinguish that from
the real site's actual certificate. Trusting this CA is equivalent to
trusting the proxy operator to decrypt and observe any TLS connection to any
site, not merely the sites this project chooses to bump. That trust is
concentrated entirely in the private key at `$SSL_DIR/ca_key.pem`, which
"never leaves this container" (per `entrypoint.sh`'s comment) but is
included in the backup archive described in the README (so restoring a
backup restores the same trusted identity — devices don't need to re-trust a
new cert after a restore, which is convenient operationally but also means
that backup archive is exactly as sensitive as the key itself).

### Why a device that doesn't trust the cert sees a warning for bump-mode sites

For any domain in `bump` mode (Crunchyroll, or any admin-configured domain —
see `sni_helper.py`'s `handle_bump`), `ssl_bump bump step2 sni_bump` in
`squid.conf.template` means Squid fully terminates the TLS connection and
re-encrypts it with a leaf cert signed by the local CA. If the connecting
device has not installed/trusted that CA as a root, its TLS stack correctly
identifies the presented certificate as untrusted (signed by an unknown CA)
and shows the browser's standard "connection not private" warning.

**This is a designed-in signal, not a bug.** Both the README and
`SETTINGS_BODY` in `dashboard.py` describe it as a diagnostic: "if a
`bump`-mode site like Crunchyroll already loads cleanly on a device, that
device's certificate trust is set up correctly." The `block_page_mode`
setting (`terminate` vs. `redirect`, see `sni_helper.py`'s
`handle_block_page` and `db.get_setting(conn, "block_page_mode", "terminate")`)
explicitly trades on this: switching to `redirect` bumps *every* blocked
connection (including previously-untouched `splice`-mode ones) so a friendly
deny page can be served — but only produces a clean page if the device
already trusts the CA; otherwise it produces the same "not private" warning,
which the docs call out as strictly worse than the plain connection failure
`terminate` mode produces. This is why `terminate` is the default and the
Settings page warns not to switch to `redirect` until certificate trust is
confirmed fleet-wide.

### Why `splice` mode never has this issue

`splice`-mode domains (the default mode) are handled by
`ssl_bump splice step2 sni_splice_allowed` — Squid inspects only the
ClientHello's SNI field during the TLS handshake (`ssl_bump peek step1`,
then the SNI-based ACLs in `sni_helper.py`'s `handle_splice`/`handle_trusted`)
and, if allowed, **passes the encrypted bytes through unmodified** — it never
terminates the TLS session, never presents a substitute certificate, and
never sees anything beyond the hostname in plaintext. Because the client's
TLS handshake completes directly with the real origin server using the real
origin's real certificate, there is nothing for the client to distrust:
splice mode is transport-layer forwarding, not interception, so no
certificate warning can ever occur for it. This is the whole reason
`trusted` and `splice` domains exist as separate modes from `bump` — they
get per-host filtering with none of the CA-trust prerequisite or MITM
exposure that `bump` mode inherently carries.

---

## 6. Rate-limiting, lockout, brute-force protection

**There is none, anywhere in this codebase**, and this is worth an extending
agent knowing explicitly before pointing any part of this system at the
internet:

- **Dashboard admin login** (`require_admin` / `_check_admin_auth` in
  `dashboard/dashboard.py`): no failed-attempt counter, no delay, no
  lockout, no CAPTCHA. An attacker with network access to the dashboard port
  can attempt unlimited HTTP Basic credential guesses. PBKDF2 at 260,000
  iterations (§1) raises the cost of guessing per attempt, but nothing caps
  the number of attempts.
- **Per-person proxy login** (`basic_auth_helper.py` via Squid's
  `auth_param basic`): same situation — Squid's `credentialsttl 4 hours`
  only controls how long a *successful* auth is cached, not how many failed
  attempts are permitted. No lockout on repeated bad passwords.
- **No IP-based throttling** anywhere in `common/squid_helper.py`,
  `matching.py`, or `dashboard.py`.

The only two mitigating factors present are architectural, not
brute-force-specific: (a) `_check_admin_auth`'s password comparison is
constant-time (`hmac.compare_digest` inside `verify_password`), removing a
timing side-channel, and (b) the LAN-scoping described in §7, which — while
it is *not itself authentication* and is explicitly not one — currently
limits who can even reach these login prompts to begin with in the intended
LAN-only deployment. If this system is ever deployed such that either login
surface (dashboard or proxy) is reachable from the internet, brute-force
protection (rate limiting, lockout, fail2ban-style IP banning, or a WAF in
front) would need to be added — nothing in the current code provides it.

---

## 7. Scoping / trust boundaries — LAN-only design

**Where:** `common/matching.py`'s `ip_in_configured_lan()`; `local_network`
setting seeded in `proxy/entrypoint.sh` and `dashboard/dashboard.py`'s
`bootstrap_admin()`; documented caveats in `.env.example` and
`README.md`.

### The LAN-IP check as a defense layer

`matching.ip_in_configured_lan(conn, ip_str)` checks the connecting client's
IP against the `local_network` setting — one or more space-separated CIDRs
(e.g. `192.168.1.0/24`). It is consulted in two places: `authz_helper.py`'s
`decide()` (HTTP-layer, bump-mode domains) and `sni_helper.py`'s
`handle_splice()` (SNI-layer, splice-mode domains) — both deny with
`reason="outside_lan"` if the client IP doesn't match. This is explicitly a
**second, independent layer on top of** per-person proxy authentication
(§3), not a replacement for it: a correct username/password from outside the
configured range is still denied.

An empty `local_network` setting **disables this check entirely** —
`ip_in_configured_lan()`'s docstring and code both treat a blank value as
"check disabled" (`return True`), falling back to per-person proxy logins as
the sole gate.

### The Docker networking caveat

`.env.example` documents why an operator might need to disable this check:

> NOTE: this check only works when the proxy sees real client IPs, i.e. with
> host networking on Linux. Under Docker Desktop (Windows/Mac) or plain
> bridge networking the proxy sees an internal gateway address instead, and
> this would reject every request. In that case leave it blank (here or in
> the dashboard) to disable the check and rely on the per-person proxy
> logins alone.

This is echoed in the Settings page hint text in `dashboard.py`
(`SETTINGS_BODY`) and in `README.md`. **An agent changing networking mode
(e.g. adding a bridge-mode Docker Compose profile) should recognize that
doing so silently makes this defense layer unable to distinguish LAN clients
from anything else**, and that the documented mitigation is disabling the
check outright and accepting that proxy-login credentials are the only
remaining gate.

### Current LAN-only assumptions worth revisiting before any internet-facing deployment

The codebase and docs consistently assume a home-LAN deployment; concretely,
what's evidenced in code:

- `DASHBOARD_BIND` defaults to `127.0.0.1` in `.env.example` and
  `docker-compose.yml` ("dashboard only reachable from this machine — use
  SSH port-forwarding for remote access"); `0.0.0.0` is offered as the
  alternative for "reachable from any device on your LAN," not from the
  internet.
- The dashboard is served over **plain HTTP** via `waitress.serve(app,
  host=host, port=port, threads=8)` in `dashboard.py`'s `main()` — there is
  no TLS termination anywhere in the dashboard container. Admin credentials
  (HTTP Basic, §1) and every form submission travel unencrypted between
  browser and dashboard. This is a reasonable trade for a LAN-only tool but
  would need a reverse proxy with TLS (or equivalent) in front before any
  exposure beyond a trusted LAN/VPN.
- The LAN-IP check (§7) is treated throughout the codebase as a meaningful
  trust signal on top of per-login auth — but per `.env.example`, it is
  routinely disabled outright under Docker Desktop/bridge networking,
  leaving per-person proxy logins (§3, with no brute-force protection, §6)
  as the sole gate in that configuration.
- No rate limiting anywhere (§6) — acceptable when the only reachable
  parties are already on the trusted LAN, not acceptable once either login
  surface is reachable from an untrusted network.
- Squid's `ssl_bump` CA trust model (§5) assumes the operator controls and
  can push CA trust to every device on the network (README's per-device
  certificate-trust install steps) — a workable assumption for a household
  under one administrative control, not for an arbitrary internet-facing
  user base.

None of this is a defect in what the project claims to be — README and
`.env.example` are explicit that this is a home/LAN parental-control tool —
but any future work aimed at internet-facing or multi-household/multi-tenant
use would need to add: TLS on the dashboard, brute-force protection on both
login surfaces (§6), and a reconsideration of what "LAN membership" is even
supposed to mean as a trust signal once clients aren't all on one
administratively-controlled network.
