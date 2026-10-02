"""`ordo doctor` refuses an SSO allowlist nobody can sign in with (ordo/host/doctor.py).

A placeholder address (me@example.com) satisfies the render's required key but admits no one: the
edge then answers every Google sign-in with oauth2-proxy's 403 page. That once sat in the live
source unnoticed until the operator was locked out, so doctor names it.
"""
from __future__ import annotations

from types import SimpleNamespace

from ordo.host import doctor


def rendered(emails: str | None):
    files = {} if emails is None else {"emails.txt": "\n".join(e for e in emails.split(",") if e) + "\n"}
    return SimpleNamespace(sso_allowlist_files=lambda: files)


def test_real_addresses_pass():
    ok, line = doctor.sso_allowlist_check(rendered("cam@min-max.tech,someone@gmail.com"))
    assert ok, line
    assert "2 address" in line


def test_placeholder_domains_fail():
    for placeholder in ("me@example.com", "you@example.org", "a@example.net", "x@host.example", "y@foo.invalid",
                        "z@bar.test", "w@localhost"):
        ok, line = doctor.sso_allowlist_check(rendered(f"cam@min-max.tech,{placeholder}"))
        assert not ok, placeholder
        assert "placeholder" in line


def test_an_empty_allowlist_fails():
    ok, line = doctor.sso_allowlist_check(rendered(""))
    assert not ok
    assert "no address" in line


def test_edge_off_is_fine():
    ok, line = doctor.sso_allowlist_check(rendered(None))
    assert ok
    assert "edge off" in line
