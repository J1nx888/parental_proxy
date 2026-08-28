"""Regression test for a real bug found running against a live Squid
instance (2026-08-28 smoke test): every external_acl_type FORMAT string in
squid.conf.template was missing an explicit %DATA macro. Squid always
appends %DATA to an external_acl_type FORMAT unless it's already present
(documented Squid behavior) -- so every helper was actually receiving one
more field on the wire than its registered field_count expected, making
every single line "malformed" and rejected as ERR. In practice this meant
the entire SNI/authz decision layer never worked at all: every real request
fell through to squid.conf's `ssl_bump terminate step2 all` catch-all.

This test parses squid.conf.template's actual FORMAT lines and cross-checks
them against the field_count each helper's main() registers with
squid_helper.run(), so a future edit to either side alone (adding a %macro
to the config without updating the helper, or vice versa) fails loudly here
instead of silently breaking every request against a real Squid.
"""
from __future__ import annotations

import re
from pathlib import Path

import authz_helper
import sni_helper
import squid_helper

TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "proxy" / "squid.conf.template"


def _expected_field_count(acl_name: str) -> int:
    """How many fields Squid will actually send on the wire for the named
    external_acl_type: its FORMAT line's %macro count, plus 1 for the
    implicit %DATA Squid appends unless %DATA is already present."""
    text = TEMPLATE_PATH.read_text()
    # `external_acl_type NAME options... \` then `FORMAT... \` then the helper
    # command line -- two backslash-continued lines.
    pattern = re.compile(
        rf"external_acl_type\s+{re.escape(acl_name)}\s+.*?\\\s*\n(.*?)\\\s*\n",
        re.DOTALL,
    )
    match = pattern.search(text)
    assert match, f"could not find external_acl_type {acl_name!r} in {TEMPLATE_PATH}"
    format_line = match.group(1)
    macros = re.findall(r"%\S+", format_line)
    assert macros, f"no %macros found for {acl_name}: {format_line!r}"
    return len(macros) if "%DATA" in macros else len(macros) + 1


def _captured_field_count(monkeypatch, call) -> int:
    captured: dict = {}

    def fake_run(name, field_count, handler, **kwargs):
        captured["field_count"] = field_count
        return 0

    # sni_helper and authz_helper both `import squid_helper` -- same cached
    # module object, so patching it here intercepts either's main().
    monkeypatch.setattr(squid_helper, "run", fake_run)
    call()
    assert "field_count" in captured, "squid_helper.run was never called"
    return captured["field_count"]


def test_sni_helper_field_count_matches_squid_conf_template(monkeypatch):
    for mode, acl_name in [
        ("bump", "sni_bump_check"),
        ("trusted", "sni_trusted_check"),
        ("splice", "sni_splice_check"),
        ("block_page", "sni_block_page_check"),
    ]:
        monkeypatch.setattr("sys.argv", ["sni_helper.py", mode])
        actual = _captured_field_count(monkeypatch, sni_helper.main)
        expected = _expected_field_count(acl_name)
        assert actual == expected, (
            f"sni_helper.py {mode}: registered field_count={actual} but "
            f"squid.conf.template's {acl_name} FORMAT actually sends {expected} fields"
        )


def test_authz_helper_field_count_matches_squid_conf_template(monkeypatch):
    actual = _captured_field_count(monkeypatch, authz_helper.main)
    expected = _expected_field_count("authz_check")
    assert actual == expected, (
        f"authz_helper.py: registered field_count={actual} but "
        f"squid.conf.template's authz_check FORMAT actually sends {expected} fields"
    )


def test_basic_auth_is_not_affected_by_the_data_macro_rule():
    """auth_param basic (not external_acl_type) uses a different protocol --
    Squid does not append %DATA to it. Documents why basic_auth_helper.py's
    field_count=2 is correct and was never part of this bug."""
    text = TEMPLATE_PATH.read_text()
    assert "auth_param basic program" in text
    assert "%DATA" not in text.split("auth_param basic program")[1].split("\n")[0]
