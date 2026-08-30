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
    monkeypatch.setattr(adguard_client._OPENER, "open", lambda r, timeout=None: _json_response({}))
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
