#!/usr/bin/env python3
"""Squid `external_acl_type` helper for ssl_bump step2 decisions.

Invoked four different ways from squid.conf (same script, different mode
argument), each backing one ssl_bump rule, evaluated in this order:

    sni_helper.py trusted     -> OK if mode='trusted' (always spliced, unchecked)
    sni_helper.py bump        -> OK if mode='bump' (always fully decrypted)
    sni_helper.py splice      -> OK if mode='splice' AND this user may access it
    sni_helper.py block_page  -> catch-all for everything else (unrecognized
                                  domain, or a splice-mode domain this user
                                  can't access). OK if the admin's
                                  block_page_mode setting is 'redirect' --
                                  this deliberately bumps (decrypts) just
                                  this one connection so a real HTTP deny
                                  page can be served (via authz_helper.py's
                                  existing deny path) instead of a bare
                                  connection failure. If the setting is
                                  'terminate', always ERR here, so the
                                  connection falls through to squid.conf's
                                  final `ssl_bump terminate step2 all` and
                                  is never decrypted at all -- the tradeoff
                                  is a broken-looking connection instead of
                                  an explanation.

Protocol (external_acl_type, format `%LOGIN %>a %ssl::>sni %DATA`): one line
per request, four percent-encoded fields, respond "OK" or "ERR". The trailing
%DATA field is always "-" (no `acl ... external ...` line below passes a
static argument) and is otherwise unused -- it must still be declared and
consumed, because Squid always appends %DATA to an external_acl_type FORMAT
that doesn't already include it (see squid.conf.template's comment).

'splice' mode logs every decision at this stage -- spliced connections are
never decrypted, so this is the only point that traffic is ever observed at
all. 'bump' mode and the 'block_page' bump-for-denial path both log richly
at the HTTP layer instead (authz_helper.py, once decrypted). 'block_page'
also logs a domain-only entry itself, but *only* for a genuinely
unconfigured domain when not in 'redirect' mode -- otherwise that case
would never be recorded anywhere, since nothing downstream ever runs to
log it either (GH #1). 'trusted' mode is deliberately never logged (see
project README).
"""
from __future__ import annotations

import sys

sys.path.insert(0, "/opt/parental-proxy")

import db
import logging_util
import matching
import squid_helper


def handle_bump(conn, login: str, client_ip: str, sni: str, _data: str = "-") -> bool:
    domain = matching.find_domain(conn, sni)
    return domain is not None and domain["mode"] == "bump"


def handle_trusted(conn, login: str, client_ip: str, sni: str, _data: str = "-") -> bool:
    domain = matching.find_domain(conn, sni)
    return domain is not None and domain["mode"] == "trusted"


def _log_denial(conn, login: str, sni: str, reason: str) -> None:
    """Log a denied SNI-layer decision -- domain only, no path, since
    nothing is decrypted at this layer. Shared by handle_splice and
    handle_block_page so the login-to-(user_id, username) resolution for
    logging isn't duplicated between them."""
    if login in ("-", "", None):
        logging_util.log_access(
            conn, user_id=None, username="(unauthenticated)", domain=sni,
            path=None, allowed=False, reason=reason,
        )
    else:
        user = matching.get_user_by_username(conn, login)
        logging_util.log_access(
            conn, user_id=user["id"] if user else None, username=login,
            domain=sni, path=None, allowed=False, reason=reason,
        )


def handle_splice(conn, login: str, client_ip: str, sni: str, _data: str = "-") -> bool:
    domain = matching.find_domain(conn, sni)
    if domain is None or domain["mode"] != "splice":
        return False

    if login in ("-", "", None):
        _log_denial(conn, login, sni, "not_authenticated")
        return False

    if not matching.ip_in_configured_lan(conn, client_ip):
        logging_util.log_access(
            conn, user_id=None, username=login, domain=sni,
            path=None, allowed=False, reason="outside_lan",
        )
        return False

    user = matching.get_user_by_username(conn, login)
    if user is None:
        return False

    allowed = bool(domain["is_global"]) or matching.user_has_domain(conn, user["id"], domain["id"])
    logging_util.log_access(
        conn, user_id=user["id"], username=login, domain=sni, path=None,
        allowed=allowed, reason="global_domain" if domain["is_global"] else "user_domain",
    )
    return allowed


def handle_block_page(conn, login: str, client_ip: str, sni: str, _data: str = "-") -> bool:
    # Reached only for connections none of the other three rules matched --
    # i.e. this is already going to be denied one way or another. The only
    # question is whether we bump it to explain that, or terminate outright.
    # (No login/LAN check needed here: authz_helper.py will independently
    # deny this once decrypted regardless, and http_access already requires
    # authentication before any ssl_bump step is reached at all.)
    mode = db.get_setting(conn, "block_page_mode", "terminate")

    # Whenever this connection is going to be denied without decryption --
    # i.e. any mode value other than 'redirect', not just the literal string
    # 'terminate' -- this is the only point in the whole SNI-layer decision
    # chain that can ever record a genuinely *unconfigured* domain:
    # sni_bump/sni_trusted/sni_splice_allowed each require a matching
    # `domains` row before doing anything, so none of them log one, and
    # authz_helper.py never runs either since nothing gets decrypted.
    # Without this, an unconfigured domain a kid tries is completely
    # invisible on the Report page under the safe default -- no way to
    # reactively approve it (GH #1). Matching the actual deny condition
    # (`mode == "redirect"` below) rather than only the expected
    # "terminate" value means an unrecognized/corrupted setting still gets
    # logged instead of silently reintroducing this same blind spot.
    #
    # Skip logging when a domain row *does* exist: a configured splice-mode
    # domain the user isn't permitted is already logged by handle_splice
    # before this rule is ever reached, so logging again here would just be
    # a duplicate (and a worse one -- no LAN/auth-specific reason).
    #
    # Skip logging entirely in 'redirect' mode: that path bumps the
    # connection so authz_helper.decide() logs this same case
    # (reason="unknown_domain") with the real path attached, which is
    # strictly better information. If the admin switches from 'terminate'
    # to 'redirect' between two attempts at the same domain, log_access()
    # lets that later, richer entry through even though a path-less one
    # from this layer was already logged for the same key -- see
    # log_access()'s docstring/comment.
    # Cost note (GH #7): this runs a domain lookup, sometimes a user lookup,
    # and a log_access() read+write for every connection to any unconfigured
    # domain -- including ordinary ad/tracker/CDN noise, not just
    # meaningful "kid tried a new site" attempts. That's the accepted
    # tradeoff of making this visible at all (GH #1); the per-process
    # find_domain() call also can't be shared with the other three
    # sni_helper modes, since each mode is a separate long-lived helper
    # process with no memory in common. See GH #7 for the tradeoffs on
    # fixing this properly; revisit if it shows up as real load or
    # Report-page noise in practice.
    if mode != "redirect" and matching.find_domain(conn, sni) is None:
        _log_denial(conn, login, sni, "unknown_domain")

    return mode == "redirect"


HANDLERS = {
    "bump": handle_bump,
    "trusted": handle_trusted,
    "splice": handle_splice,
    "block_page": handle_block_page,
}


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in HANDLERS:
        print("usage: sni_helper.py {bump|trusted|splice|block_page}", file=sys.stderr)
        return 2
    mode = sys.argv[1]
    return squid_helper.run(f"sni_helper[{mode}]", 4, HANDLERS[mode])


if __name__ == "__main__":
    raise SystemExit(main())

