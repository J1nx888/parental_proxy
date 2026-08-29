"""common/sdnotify.py: minimal systemd sd_notify protocol client."""
from __future__ import annotations

import os
import socket

import pytest

import sdnotify

af_unix_only = pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"), reason="AF_UNIX not available on this platform"
)


def test_notify_returns_false_when_notify_socket_unset(monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    assert sdnotify.notify("READY=1") is False


def test_watchdog_usec_returns_none_when_unset(monkeypatch):
    monkeypatch.delenv("WATCHDOG_USEC", raising=False)
    assert sdnotify.watchdog_usec() is None


def test_watchdog_usec_parses_integer(monkeypatch):
    monkeypatch.setenv("WATCHDOG_USEC", "15000000")
    assert sdnotify.watchdog_usec() == 15000000


def test_watchdog_usec_returns_none_for_garbage(monkeypatch):
    monkeypatch.setenv("WATCHDOG_USEC", "not-a-number")
    assert sdnotify.watchdog_usec() is None


@af_unix_only
def test_notify_sends_real_datagram_to_notify_socket(monkeypatch, tmp_path):
    sock_path = str(tmp_path / "notify.sock")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    listener.bind(sock_path)
    listener.settimeout(2)
    try:
        monkeypatch.setenv("NOTIFY_SOCKET", sock_path)
        assert sdnotify.ready() is True
        data, _ = listener.recvfrom(1024)
        assert data == b"READY=1"
    finally:
        listener.close()


@af_unix_only
def test_notify_handles_abstract_namespace_socket(monkeypatch):
    # Most real systemd services actually use an abstract-namespace
    # socket ("@name", no filesystem path) for NOTIFY_SOCKET, not a
    # real path -- worth testing explicitly since it needs the "@" ->
    # NUL-byte translation sdnotify.py does.
    name = "pp_test_notify_" + os.urandom(4).hex()
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    listener.bind("\0" + name)
    listener.settimeout(2)
    try:
        monkeypatch.setenv("NOTIFY_SOCKET", "@" + name)
        assert sdnotify.watchdog() is True
        data, _ = listener.recvfrom(1024)
        assert data == b"WATCHDOG=1"
    finally:
        listener.close()
