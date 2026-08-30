#!/usr/bin/env python3
"""Thin REST client for AdGuard Home's `/control` API.

This is what makes the "two independent axes" hard-deny invariant real
(RoadMap.md, locked 2026-08-30): a domain marked `domains.mode = 'bump'`
must never resolve normally for a device that isn't `bump_enabled` --
nftables has no domain visibility to enforce this itself (see
knftables_adapter.go's own comment), so it has to happen here, at the
DNS tier, before a connection to that domain is even attempted.

Verified live 2026-08-30 against a real AdGuard Home v0.107.79 instance
-- not written from documentation alone, matching this project's own
repeated lesson that a first draft written from memory/docs tends to be
subtly wrong in exactly the way that only shows up against the real
thing (the ARP worker's mdlayher/arp adapter, the nftables adapter's
import path, three separate Squid startup bugs this same session):
- `/control/filtering/set_rules` (POST, body `{"rules": [...]}`) does a
  full replace of AdGuard's custom filtering rules -- no incremental
  add/remove API exists, matching this project's own established
  full-reconcile pattern (phase3/nftables-manager's flush-before-re-add,
  controller's full DesiredPolicy recompute every cycle).
- `/control/filtering/status` (GET) echoes the current list back under
  a `user_rules` key.
- HTTP Basic Auth works directly against `/control/*` for a configured
  instance -- no cookie-based `/control/login` session dance needed.
- The `$client=ip1,ip2` modifier on a custom rule does exactly what it
  says: confirmed with two real client containers against a real
  instance, one resolving a domain normally and the other -- named in
  the rule's $client list -- getting `0.0.0.0` back for the identical
  query.
- `/control/install/configure` (POST) is what the first-run setup
  wizard itself calls -- NOT the bare `/install/configure` path some of
  AdGuard's own generated OpenAPI-doc tooling implies (every route
  lives under `/control`, even before the instance is configured at
  all). It writes a complete, correctly-versioned AdGuardHome.yaml
  itself; see adguard/entrypoint.sh, which calls this once on first
  boot instead of hand-templating that file (confirmed live that a
  hand-authored version, built from the wiki's documented fields alone,
  was missing several fields the real binary always writes --
  `session_ttl` format, `upstream_mode`, `cache_optimistic_*`,
  `doh.routes` -- so letting AdGuard build its own config is both
  simpler and version-correct in a way a template can't be kept in sync
  with automatically).
- `/control/filtering/status`'s `user_rules` key is present but `null`,
  not `[]`, on a freshly-configured instance that has never had a custom
  rule set -- confirmed live 2026-08-30 running the full interception
  stack against a brand-new AdGuard container for the first time ever
  (every earlier live AdGuard test this project ran had already pushed
  at least one custom rule by the time `get_custom_rules` was exercised,
  which is exactly why this hadn't surfaced before). `get_custom_rules`
  below treats that ONE specific shape -- key present, value `null` --
  as "no rules yet" (empty list), same as `[]`. It deliberately does NOT
  extend the same treatment to a missing key or a non-object response:
  an early version of this fix did, and a code review the same day
  caught that this let `sync_once()` silently proceed to a destructive
  full-replace write on what could be a genuinely malformed response
  (wrong endpoint, incompatible AdGuard version) rather than the
  confirmed benign case -- those still raise `AdGuardError` and fail
  closed, exactly as before this fix existed.

No third-party dependencies -- matches common/cr_api.py's own
urllib-based pattern, mirrored here, rather than adding `requests` to a
package this project otherwise keeps stdlib-only outside dashboard/.
"""
from __future__ import annotations

import base64
import json
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

DEFAULT_TIMEOUT = 5.0
MAX_RESPONSE_BYTES = 1 * 1024 * 1024

# Bypass environment proxy variables -- this client always talks
# directly to AdGuard Home's own control port, never through Squid or
# anything else that might itself depend on AdGuard being reachable.
_OPENER = build_opener(ProxyHandler({}))


class AdGuardError(RuntimeError):
    """Raised for any failure talking to AdGuard Home -- unreachable,
    a non-2xx response, or a malformed body. Callers (adguard_sync.py)
    must treat this the same way controller/discovery.py treats a
    failed snapshot: log it and retry next cycle, never crash the
    process over a transient AdGuard restart or network hiccup."""


def _basic_auth_header(username: str, password: str) -> str:
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def _request(
    url: str,
    *,
    method: str,
    username: str | None = None,
    password: str | None = None,
    json_body: dict | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> bytes:
    headers: dict[str, str] = {}
    data = None
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if username is not None:
        headers["Authorization"] = _basic_auth_header(username, password or "")

    request = Request(url, data=data, headers=headers, method=method)
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            return response.read(MAX_RESPONSE_BYTES)
    except HTTPError as exc:
        detail = exc.read(300).decode("utf-8", errors="replace")
        raise AdGuardError(f"HTTP {exc.code} from {url}: {detail}") from exc
    except URLError as exc:
        raise AdGuardError(f"could not reach {url}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise AdGuardError(f"request to {url} timed out") from exc


def get_custom_rules(
    base_url: str, username: str, password: str, timeout: float = DEFAULT_TIMEOUT
) -> list[str]:
    """The CURRENT custom filtering rules list, straight from AdGuard --
    never cached, same "always read fresh" discipline as
    policy.ActualPolicy on the nftables side. adguard_sync.py reads this
    first so it can preserve any rules it doesn't manage (an admin's own
    manually-added rule) before overwriting with set_custom_rules."""
    body = _request(
        f"{base_url.rstrip('/')}/control/filtering/status",
        method="GET",
        username=username,
        password=password,
        timeout=timeout,
    )
    try:
        decoded = json.loads(body)
    except ValueError as exc:
        raise AdGuardError(f"malformed JSON from {base_url}/control/filtering/status: {exc}") from exc
    if not isinstance(decoded, dict):
        raise AdGuardError(f"{base_url}/control/filtering/status didn't return a JSON object")
    if "user_rules" not in decoded:
        raise AdGuardError("AdGuard Home's filtering/status response had no 'user_rules' key")
    rules = decoded["user_rules"]
    if rules is None:
        # A freshly-configured instance that's never had a custom rule set
        # reports the KEY PRESENT but `null`, not `[]` -- see this module's
        # docstring. Treat that one specific, confirmed-live shape as an
        # empty list rather than raising, or sync_once() could never
        # complete its very first cycle against a brand-new AdGuard
        # instance (it always reads before it writes). Deliberately narrow:
        # a non-dict response or a missing key entirely is a genuinely
        # different, more anomalous failure (wrong endpoint, incompatible
        # AdGuard version) and should still raise and fail closed --
        # collapsing all three into one silent "no rules yet" used to let
        # sync_once() proceed to a full-replace write on a merely
        # malformed read, silently discarding any admin-added rules that
        # were actually still there (found via code review 2026-08-30).
        rules = []
    if not isinstance(rules, list):
        raise AdGuardError("AdGuard Home's filtering/status response had a non-list 'user_rules'")
    return [str(r) for r in rules]


def set_custom_rules(
    base_url: str, username: str, password: str, rules: list[str], timeout: float = DEFAULT_TIMEOUT
) -> None:
    """Full replace of AdGuard Home's custom filtering rules list -- see
    this module's docstring for why there's no incremental API. Callers
    must pass the COMPLETE desired list (including any preserved
    not-ours rules from get_custom_rules), not just the ones they own."""
    _request(
        f"{base_url.rstrip('/')}/control/filtering/set_rules",
        method="POST",
        username=username,
        password=password,
        json_body={"rules": rules},
        timeout=timeout,
    )


def set_filters_update_interval(
    base_url: str, username: str, password: str, interval_hours: int, timeout: float = DEFAULT_TIMEOUT
) -> None:
    """Sets how often AdGuard Home ITSELF re-checks every subscribed
    filter list (its own built-in background schedule -- this project
    doesn't reimplement update-checking, just configures AdGuard's real
    one). `adguard/entrypoint.sh` calls this once during first-run
    bootstrap with `interval_hours=168` (one week, matching AdGuard's
    own "Once a week" UI preset) -- confirmed live 2026-08-30 that 168
    is accepted and echoed back exactly by `/control/filtering/status`.
    """
    _request(
        f"{base_url.rstrip('/')}/control/filtering/config",
        method="POST",
        username=username,
        password=password,
        json_body={"enabled": True, "interval": interval_hours},
        timeout=timeout,
    )


def refresh_filters(base_url: str, username: str, password: str, timeout: float = DEFAULT_TIMEOUT) -> int:
    """Forces AdGuard Home to check every subscribed filter list right
    now, instead of waiting for its own update interval -- this is what
    the dashboard's "Check for filter updates now" button calls.
    Confirmed live 2026-08-30 this is safe to call as often as wanted
    (AdGuard's own docs: "ratelimited, so you can call it freely").
    Returns how many lists actually had new content -- 0 is a normal,
    healthy result when nothing has changed upstream since the last
    check, not a failure.
    """
    body = _request(
        f"{base_url.rstrip('/')}/control/filtering/refresh",
        method="POST",
        username=username,
        password=password,
        json_body={"whitelist": False},
        timeout=timeout,
    )
    try:
        decoded = json.loads(body)
    except ValueError as exc:
        raise AdGuardError(f"malformed JSON from {base_url}/control/filtering/refresh: {exc}") from exc
    updated = decoded.get("updated") if isinstance(decoded, dict) else None
    if not isinstance(updated, int):
        raise AdGuardError("AdGuard Home's filtering/refresh response had no 'updated' count")
    return updated
