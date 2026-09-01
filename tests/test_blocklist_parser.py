"""Phase 8: common/blocklist_parser.py -- pure text extraction from the
three real formats confirmed live against
https://github.com/blocklistproject/Lists (see that module's docstring)."""
from __future__ import annotations

import blocklist_parser


def test_adguard_format():
    text = "||example.com^\n||sub.example.org^$important\n"
    assert blocklist_parser.parse_hostlist(text) == ["example.com", "sub.example.org"]


def test_hosts_format_both_null_route_ips():
    text = "0.0.0.0 example.com\n127.0.0.1 other.example.com\n"
    assert blocklist_parser.parse_hostlist(text) == ["example.com", "other.example.com"]


def test_hosts_format_ipv6_null_route():
    text = ":: example.com\n::1 other.example.com\n"
    assert blocklist_parser.parse_hostlist(text) == ["example.com", "other.example.com"]


def test_hosts_format_multiple_aliases_on_one_line():
    text = "0.0.0.0 example.com www.example.com\n"
    assert blocklist_parser.parse_hostlist(text) == ["example.com", "www.example.com"]


def test_bare_domain_per_line():
    text = "example.com\nanother.example.org\n"
    assert blocklist_parser.parse_hostlist(text) == ["example.com", "another.example.org"]


def test_full_line_comments_and_blank_lines_skipped():
    text = "# a comment\n! also a comment\n\n   \nexample.com\n"
    assert blocklist_parser.parse_hostlist(text) == ["example.com"]


def test_header_block_like_real_blocklistproject_file():
    text = (
        "! Title: Porn Block List\n"
        "! Description: Adult content domains\n"
        "! Format: AdGuard\n"
        "!\n"
        "||example-adult-site.com^\n"
    )
    assert blocklist_parser.parse_hostlist(text) == ["example-adult-site.com"]


def test_deduplicates_preserving_first_seen_order():
    text = "example.com\n||example.com^\n0.0.0.0 example.com\nzzz.example.com\n"
    assert blocklist_parser.parse_hostlist(text) == ["example.com", "zzz.example.com"]


def test_lowercases_and_strips_trailing_dot():
    text = "EXAMPLE.COM.\n"
    assert blocklist_parser.parse_hostlist(text) == ["example.com"]


def test_malformed_lines_skipped_not_raised():
    text = (
        "@@||exception.example.com^\n"  # exception rule -- not a form this parser understands
        "not a domain at all!!\n"
        "/some-regex-rule/\n"
        "example.com\n"
    )
    assert blocklist_parser.parse_hostlist(text) == ["example.com"]


def test_empty_input_returns_empty_list():
    assert blocklist_parser.parse_hostlist("") == []


def test_whitespace_only_lines_and_crlf_handled():
    text = "example.com\r\n   \r\nother.example.org\r\n"
    assert blocklist_parser.parse_hostlist(text) == ["example.com", "other.example.org"]
