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
    rules = decoded.get("user_rules") if isinstance(decoded, dict) else None
    if not isinstance(rules, list):
        raise AdGuardError("AdGuard Home's filtering/status response had no 'user_rules' list")
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
