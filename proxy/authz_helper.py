#!/usr/bin/env python3
"""Squid `external_acl_type` helper for the HTTP-layer decision on bump-mode
domains (the ones ssl_bump fully decrypts, per sni_helper.py's 'bump' check).

Protocol (format `%LOGIN %>a %DST %PATH`): one line per request, four
percent-encoded fields, respond "OK" or "ERR".

Decision order for a bump-mode domain:
  1. Client must be authenticated and inside the configured LAN.
  2. Domain must be globally allowed, or explicitly assigned to this user.
  3. If it's the Crunchyroll domain: resolve watch/playback/series requests
     to their parent show via the CMS API (cached) and check the user's
     show list. CMS metadata-only requests are always allowed (matches v1).
  4. Otherwise: if the domain has any configured allowed-paths, the
     request's path must match one of them (defense-in-depth, same idea as
     v1's allowed_paths.txt). A domain with zero configured paths allows
     any path -- admins only need to curate paths for domains where that
     matters.

Every decision is logged (deduped) via logging_util.
"""
from __future__ import annotations

import sys
import urllib.parse

sys.path.insert(0, "/opt/parental-proxy")

import cr_urls
import db
import logging_util
import matching
import series_resolve


def _split_host_port(dst: str) -> str:
    if dst.startswith("["):  # IPv6 literal, rare on a home LAN but be safe
        return dst.split("]")[0].lstrip("[")
    return dst.split(":", 1)[0]


def decide(conn, login: str, client_ip: str, dst: str, path: str) -> bool:
    hostname = _split_host_port(dst)
    path = path or "/"

    if login in ("-", "", None):
        return False

    if not matching.ip_in_configured_lan(conn, client_ip):
        logging_util.log_access(
            conn, user_id=None, username=login, domain=hostname,
            path=path, allowed=False, reason="outside_lan",
        )
        return False

    domain = matching.find_domain(conn, hostname)
    if domain is None or domain["mode"] != "bump":
        # Two ways to get here: a genuinely unconfigured domain, or a
        # splice-mode domain this user isn't permitted -- ssl_bump's
        # block_page rule (see sni_helper.py) deliberately bumps both so a
        # real deny page can be served here instead of a bare connection
        # failure. Either way: deny. Resolve the user (if there is one) so
        # this still shows up as approvable from the report.
        user = matching.get_user_by_username(conn, login)
        logging_util.log_access(
            conn, user_id=user["id"] if user else None, username=login, domain=hostname,
            path=path, allowed=False,
            reason="unknown_domain" if domain is None else "not_bump_mode",
        )
        return False

    user = matching.get_user_by_username(conn, login)
    if user is None:
        return False

    domain_ok = bool(domain["is_global"]) or matching.user_has_domain(conn, user["id"], domain["id"])
    if not domain_ok:
        logging_util.log_access(
            conn, user_id=user["id"], username=login, domain=hostname,
            path=path, allowed=False, reason="domain_not_assigned",
        )
        return False

    if domain["kind"] == "crunchyroll":
        return _decide_crunchyroll(conn, user, login, hostname, path, domain)

    if not matching.path_allowed(conn, domain["id"], path) and _has_any_path_rules(conn, domain["id"]):
        logging_util.log_access(
            conn, user_id=user["id"], username=login, domain=hostname,
            path=path, allowed=False, reason="path_not_allowed",
        )
        return False

    logging_util.log_access(
        conn, user_id=user["id"], username=login, domain=hostname,
        path=path, allowed=True,
        reason="global_domain" if domain["is_global"] else "user_domain",
    )
    return True


def _has_any_path_rules(conn, domain_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM domain_paths WHERE domain_id = ? LIMIT 1", (domain_id,)
    ).fetchone()
    return row is not None


def _decide_crunchyroll(conn, user, login: str, hostname: str, path: str, domain) -> bool:
    url = f"https://{hostname}{path}"
    request = cr_urls.classify(url)

    if request.kind is cr_urls.RequestKind.CMS_OBJECTS:
        return True  # metadata only, matches v1 behavior

    if request.kind is cr_urls.RequestKind.BLOCKED_SHAPE:
        logging_util.log_access(
            conn, user_id=user["id"], username=login, domain=hostname,
            path=path, allowed=False, reason="blocked_shape",
        )
        return False

    if request.kind is cr_urls.RequestKind.OTHER:
        # Not a recognized watch/playback/series/CMS shape. Same
        # defense-in-depth v1 had: fall back to the configured path
        # allowlist for this domain instead of allowing blindly, so an
        # endpoint the classifier doesn't know about isn't automatically
        # open. A domain with zero configured paths allows anything (see
        # module docstring), matching how domain_paths behaves everywhere
        # else -- for Crunchyroll specifically, defaults.py seeds this
        # domain with a real path list, so that permissive fallback
        # shouldn't normally be reached here.
        if not _has_any_path_rules(conn, domain["id"]) or matching.path_allowed(conn, domain["id"], path):
            return True
        logging_util.log_access(
            conn, user_id=user["id"], username=login, domain=hostname,
            path=path, allowed=False, reason="path_not_allowed",
        )
        return False

    if request.kind is cr_urls.RequestKind.SERIES_PAGE:
        allowed = True
        for series_id in request.ids:
            show_ok = matching.user_has_show(conn, user["id"], series_id)
            logging_util.log_access(
                conn, user_id=user["id"], username=login, domain=hostname,
                path=path, allowed=show_ok,
                reason="show_approved" if show_ok else "show_not_approved",
                series_id=series_id,
            )
            if not show_ok:
                allowed = False
        return allowed

    # WATCH_PAGE / PLAYBACK: resolve object IDs to their parent series first.
    resolved = series_resolve.resolve_series_ids(conn, request.ids)
    if resolved is None:
        logging_util.log_access(
            conn, user_id=user["id"], username=login, domain=hostname,
            path=path, allowed=False, reason="resolution_failed",
        )
        return False

    ok = True
    for object_id in request.ids:
        series_id = resolved.get(object_id)
        show_ok = series_id is not None and matching.user_has_show(conn, user["id"], series_id)
        logging_util.log_access(
            conn, user_id=user["id"], username=login, domain=hostname,
            path=path, allowed=show_ok,
            reason="show_approved" if show_ok else "show_not_approved",
            series_id=series_id,
        )
        if not show_ok:
            ok = False
    return ok


def main() -> int:
    conn = db.get_conn()
    db.init_db(conn)

    for line in sys.stdin:
        parts = line.strip().split()
        if len(parts) != 4:
            sys.stdout.write("ERR\n")
            sys.stdout.flush()
            continue
        login, client_ip, dst, path = (urllib.parse.unquote(p) for p in parts)
        try:
            ok = decide(conn, login, client_ip, dst, path)
        except Exception as exc:
            print(f"authz_helper error: {exc}", file=sys.stderr, flush=True)
            ok = False
        sys.stdout.write(("OK\n" if ok else "ERR\n"))
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
