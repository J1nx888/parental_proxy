"""common/adguard_client.py: the REST client for AdGuard Home's `/control`
API. All network access goes through adguard_client._OPENER.open, which
every test here replaces with a fake -- same pattern as test_cr_api.py.
"""
from __future__ import annotations

import base64
import json

import pytest
from urllib.error import HTTPError, URLError

import adguard_client


class FakeResponse:
    """Minimal stand-in for the context-manager object urlopen()/opener.open()
    returns -- only `.read(n)` and context-manager protocol are used by
    adguard_client._request."""

    def __init__(self, body: bytes):
        self._body = body

    def read(self, n: int = -1) -> bytes:
        return self._body if n is None or n < 0 else self._body[:n]

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _json_response(payload) -> FakeResponse:
    return FakeResponse(json.dumps(payload).encode("utf-8"))


def test_get_custom_rules_returns_user_rules(monkeypatch):
    captured = {}

    def fake_open(request, timeout=None):
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["auth"] = request.get_header("Authorization")
        return _json_response({"enabled": True, "user_rules": ["||example.com^", "@@||ok.com^"]})

    monkeypatch.setattr(adguard_client._OPENER, "open", fake_open)

    rules = adguard_client.get_custom_rules("http://127.0.0.1:3000", "admin", "hunter2")

    assert rules == ["||example.com^", "@@||ok.com^"]
    assert captured["url"] == "http://127.0.0.1:3000/control/filtering/status"
    assert captured["method"] == "GET"
    assert captured["auth"] == "Basic " + base64.b64encode(b"admin:hunter2").decode()


def test_get_custom_rules_strips_trailing_slash_from_base_url(monkeypatch):
    captured = {}

    def fake_open(request, timeout=None):
        captured["url"] = request.full_url
        return _json_response({"user_rules": []})

    monkeypatch.setattr(adguard_client._OPENER, "open", fake_open)
    adguard_client.get_custom_rules("http://127.0.0.1:3000/", "admin", "x")
    assert captured["url"] == "http://127.0.0.1:3000/control/filtering/status"


def test_get_custom_rules_rejects_missing_user_rules_key(monkeypatch):
    # A key entirely missing is more anomalous than the confirmed-live null
    # case below (wrong endpoint, incompatible AdGuard version) -- code
    # review 2026-08-30 caught an earlier version of this fix treating this
    # the same as null, which let sync_once() silently proceed to a
    # destructive full-replace write on a merely malformed read. This must
    # still raise and fail closed.
    monkeypatch.setattr(adguard_client._OPENER, "open", lambda r, timeout=None: _json_response({}))
    with pytest.raises(adguard_client.AdGuardError, match="user_rules"):
        adguard_client.get_custom_rules("http://127.0.0.1:3000", "admin", "x")


def test_get_custom_rules_treats_null_user_rules_as_empty(monkeypatch):
    # Confirmed live 2026-08-30: a freshly-configured AdGuard Home instance
    # that has never had a custom rule set reports `"user_rules": null`
    # (key PRESENT, value null), not `[]` -- see adguard_client.py's module
    # docstring. This must NOT raise, or sync_once() could never complete
    # its first cycle ever against a brand-new instance (it always reads
    # before it writes).
    monkeypatch.setattr(adguard_client._OPENER, "open", lambda r, timeout=None: _json_response({"user_rules": None}))
    assert adguard_client.get_custom_rules("http://127.0.0.1:3000", "admin", "x") == []


def test_get_custom_rules_rejects_non_dict_top_level_response(monkeypatch):
    # A bare JSON array/scalar at the top level is a different, more
    # anomalous failure than the confirmed null-on-fresh-install quirk
    # (wrong endpoint, a proxy/auth error page that happens to be valid
    # JSON) and must still raise rather than being silently swallowed as
    # "no rules yet" -- code review 2026-08-30.
    monkeypatch.setattr(adguard_client._OPENER, "open", lambda r, timeout=None: _json_response(["not", "a", "dict"]))
    with pytest.raises(adguard_client.AdGuardError, match="JSON object"):
        adguard_client.get_custom_rules("http://127.0.0.1:3000", "admin", "x")


def test_get_custom_rules_rejects_non_list_user_rules(monkeypatch):
    monkeypatch.setattr(
        adguard_client._OPENER, "open", lambda r, timeout=None: _json_response({"user_rules": "not-a-list"})
    )
    with pytest.raises(adguard_client.AdGuardError, match="user_rules"):
        adguard_client.get_custom_rules("http://127.0.0.1:3000", "admin", "x")


def test_get_custom_rules_rejects_malformed_json(monkeypatch):
    monkeypatch.setattr(adguard_client._OPENER, "open", lambda r, timeout=None: FakeResponse(b"not json"))
    with pytest.raises(adguard_client.AdGuardError, match="malformed JSON"):
        adguard_client.get_custom_rules("http://127.0.0.1:3000", "admin", "x")


def test_set_custom_rules_posts_the_full_list(monkeypatch):
    captured = {}

    def fake_open(request, timeout=None):
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["body"] = json.loads(request.data.decode())
        captured["content_type"] = request.get_header("Content-type")
        return FakeResponse(b"")

    monkeypatch.setattr(adguard_client._OPENER, "open", fake_open)

    adguard_client.set_custom_rules(
        "http://127.0.0.1:3000", "admin", "hunter2", ["||crunchyroll.com^$client=192.168.1.10"]
    )

    assert captured["url"] == "http://127.0.0.1:3000/control/filtering/set_rules"
    assert captured["method"] == "POST"
    assert captured["body"] == {"rules": ["||crunchyroll.com^$client=192.168.1.10"]}
    assert captured["content_type"] == "application/json"


def test_set_custom_rules_with_empty_list_is_a_valid_full_replace(monkeypatch):
    captured = {}

    def fake_open(request, timeout=None):
        captured["body"] = json.loads(request.data.decode())
        return FakeResponse(b"")

    monkeypatch.setattr(adguard_client._OPENER, "open", fake_open)
    adguard_client.set_custom_rules("http://127.0.0.1:3000", "admin", "x", [])
    assert captured["body"] == {"rules": []}


def test_http_error_is_wrapped_with_status_and_detail(monkeypatch):
    def fake_open(request, timeout=None):
        raise HTTPError(request.full_url, 401, "Unauthorized", None, FakeResponse(b"bad credentials"))

    monkeypatch.setattr(adguard_client._OPENER, "open", fake_open)
    with pytest.raises(adguard_client.AdGuardError, match="HTTP 401"):
        adguard_client.get_custom_rules("http://127.0.0.1:3000", "admin", "wrong")


def test_url_error_is_wrapped_as_unreachable(monkeypatch):
    def fake_open(request, timeout=None):
        raise URLError("connection refused")

    monkeypatch.setattr(adguard_client._OPENER, "open", fake_open)
    with pytest.raises(adguard_client.AdGuardError, match="could not reach"):
        adguard_client.get_custom_rules("http://127.0.0.1:3000", "admin", "x")


def test_timeout_is_wrapped(monkeypatch):
    def fake_open(request, timeout=None):
        raise TimeoutError()

    monkeypatch.setattr(adguard_client._OPENER, "open", fake_open)
    with pytest.raises(adguard_client.AdGuardError, match="timed out"):
        adguard_client.get_custom_rules("http://127.0.0.1:3000", "admin", "x")


def test_set_filters_update_interval_posts_the_right_body(monkeypatch):
    captured = {}

    def fake_open(request, timeout=None):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data.decode())
        return FakeResponse(b"")

    monkeypatch.setattr(adguard_client._OPENER, "open", fake_open)
    adguard_client.set_filters_update_interval("http://127.0.0.1:3000", "admin", "x", 168)

    assert captured["url"] == "http://127.0.0.1:3000/control/filtering/config"
    assert captured["body"] == {"enabled": True, "interval": 168}


def test_refresh_filters_returns_the_updated_count(monkeypatch):
    monkeypatch.setattr(
        adguard_client._OPENER, "open", lambda r, timeout=None: _json_response({"updated": 3})
    )
    assert adguard_client.refresh_filters("http://127.0.0.1:3000", "admin", "x") == 3


def test_refresh_filters_zero_updated_is_a_valid_result(monkeypatch):
    monkeypatch.setattr(
        adguard_client._OPENER, "open", lambda r, timeout=None: _json_response({"updated": 0})
    )
    assert adguard_client.refresh_filters("http://127.0.0.1:3000", "admin", "x") == 0


def test_refresh_filters_posts_to_the_right_url_with_whitelist_false(monkeypatch):
    captured = {}

    def fake_open(request, timeout=None):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data.decode())
        return _json_response({"updated": 0})

    monkeypatch.setattr(adguard_client._OPENER, "open", fake_open)
    adguard_client.refresh_filters("http://127.0.0.1:3000", "admin", "x")

    assert captured["url"] == "http://127.0.0.1:3000/control/filtering/refresh"
    assert captured["body"] == {"whitelist": False}


def test_refresh_filters_rejects_a_response_with_no_updated_count(monkeypatch):
    monkeypatch.setattr(adguard_client._OPENER, "open", lambda r, timeout=None: _json_response({}))
    with pytest.raises(adguard_client.AdGuardError, match="updated"):
        adguard_client.refresh_filters("http://127.0.0.1:3000", "admin", "x")


# ============================================================
# normalize_query_log_time
# ============================================================

def test_normalize_query_log_time_truncates_nanosecond_fraction():
    # The exact shape confirmed live 2026-08-31 against a real AdGuard
    # Home instance's /control/querylog response.
    assert (
        adguard_client.normalize_query_log_time("2026-08-31T13:17:13.089285447Z")
        == "2026-08-31T13:17:13Z"
    )


def test_normalize_query_log_time_handles_no_fractional_seconds_at_all():
    assert adguard_client.normalize_query_log_time("2026-08-31T13:17:13Z") == "2026-08-31T13:17:13Z"


def test_normalize_query_log_time_handles_short_fractional_seconds():
    assert adguard_client.normalize_query_log_time("2026-08-31T13:17:13.5Z") == "2026-08-31T13:17:13Z"


def test_normalize_query_log_time_rejects_unexpected_shape():
    with pytest.raises(adguard_client.AdGuardError, match="timestamp shape"):
        adguard_client.normalize_query_log_time("not-a-timestamp")


def test_normalize_query_log_time_rejects_missing_z_suffix():
    with pytest.raises(adguard_client.AdGuardError, match="timestamp shape"):
        adguard_client.normalize_query_log_time("2026-08-31T13:17:13.089285447+00:00")


# ============================================================
# get_query_log
# ============================================================

def test_get_query_log_returns_the_data_list(monkeypatch):
    entries = [
        {"client": "192.168.1.10", "time": "2026-08-31T13:17:13.089285447Z"},
        {"client": "192.168.1.11", "time": "2026-08-31T13:17:12Z"},
    ]
    monkeypatch.setattr(
        adguard_client._OPENER, "open", lambda r, timeout=None: _json_response({"data": entries, "oldest": ""})
    )
    assert adguard_client.get_query_log("http://127.0.0.1:3000", "admin", "x") == entries


def test_get_query_log_uses_the_limit_query_param(monkeypatch):
    captured = {}

    def fake_open(request, timeout=None):
        captured["url"] = request.full_url
        return _json_response({"data": [], "oldest": ""})

    monkeypatch.setattr(adguard_client._OPENER, "open", fake_open)
    adguard_client.get_query_log("http://127.0.0.1:3000", "admin", "x", limit=25)

    assert captured["url"] == "http://127.0.0.1:3000/control/querylog?limit=25"


def test_get_query_log_rejects_a_response_missing_the_data_list(monkeypatch):
    monkeypatch.setattr(adguard_client._OPENER, "open", lambda r, timeout=None: _json_response({"oldest": ""}))
    with pytest.raises(adguard_client.AdGuardError, match="data"):
        adguard_client.get_query_log("http://127.0.0.1:3000", "admin", "x")


def test_get_query_log_rejects_a_non_dict_top_level_response(monkeypatch):
    monkeypatch.setattr(adguard_client._OPENER, "open", lambda r, timeout=None: _json_response(["not", "a", "dict"]))
    with pytest.raises(adguard_client.AdGuardError, match="data"):
        adguard_client.get_query_log("http://127.0.0.1:3000", "admin", "x")
