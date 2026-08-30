"""dashboard/block_page_server.py: the tiny stdlib-only HTTP server for
the AdGuard-side friendly block page. Real integration test -- binds an
ephemeral port and makes real HTTP requests, since this module has no
pure logic worth unit-testing in isolation (it's a handful of lines of
stdlib http.server wiring; the actual "does the request produce the
right page" behavior is what matters).
"""
from __future__ import annotations

import http.client

import pytest

import block_page_server


@pytest.fixture
def server():
    srv = block_page_server.start(host="127.0.0.1", port=0)  # port=0 -> OS picks a free one
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()


def _get(server, path="/", host_header="crunchyroll.com", method="GET"):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    try:
        conn.request(method, path, headers={"Host": host_header})
        resp = conn.getresponse()
        return resp.status, resp.read().decode("utf-8"), dict(resp.getheaders())
    finally:
        conn.close()


def test_returns_403_with_html_body(server):
    status, body, headers = _get(server)
    assert status == 403
    assert "text/html" in headers["Content-Type"]
    assert "isn't approved" in body


def test_shows_the_requested_host_in_the_page(server):
    _, body, _ = _get(server, host_header="netflix.com")
    assert "netflix.com" in body


def test_strips_a_nonstandard_port_from_the_displayed_host(server):
    _, body, _ = _get(server, host_header="netflix.com:8080")
    assert "netflix.com" in body
    assert "8080" not in body


def test_falls_back_to_a_generic_label_with_no_host_header(server):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    try:
        # http.client always sends a Host header for HTTP/1.1 -- send a
        # raw HTTP/1.0 request instead, which doesn't require one, to
        # genuinely exercise the missing-Host-header fallback.
        conn._http_vsn = 10
        conn._http_vsn_str = "HTTP/1.0"
        conn.request("GET", "/")
        resp = conn.getresponse()
        body = resp.read().decode("utf-8")
    finally:
        conn.close()
    assert "this site" in body


def test_head_and_post_also_get_the_page(server):
    status, body, _ = _get(server, method="HEAD")
    assert status == 403
    status, body, _ = _get(server, method="POST")
    assert status == 403
    assert "isn't approved" in body


def test_any_path_gets_the_same_page(server):
    status, body, _ = _get(server, path="/some/deep/path?query=1")
    assert status == 403
    assert "isn't approved" in body
