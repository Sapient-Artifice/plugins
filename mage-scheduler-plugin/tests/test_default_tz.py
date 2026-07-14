"""Tests for _default_tz — robust system-timezone detection (schemas.py).

Regression guard: a backend restarted from a shell without ``TZ`` used to
silently default all scheduling to UTC. The /etc/localtime fallback fixes that.
"""
from __future__ import annotations

import sys
from pathlib import Path

from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mage_scheduler"))

import schemas  # noqa: E402


class _NoKeyTz:
    """A fixed-offset tzinfo with no IANA .key — mimics macOS with TZ unset."""


class _Aware:
    tzinfo = _NoKeyTz()


class _FakeNow:
    def astimezone(self):
        return _Aware()


class _FakeDatetime:
    @staticmethod
    def now():
        return _FakeNow()


def _force_no_key(monkeypatch):
    """Make the astimezone().key path yield nothing, as when TZ is unset."""
    monkeypatch.setattr(schemas, "datetime", _FakeDatetime)


# ── env override ─────────────────────────────────────────────────────────

def test_env_override_wins(monkeypatch):
    monkeypatch.setenv("SCHEDULER_TIMEZONE", "Europe/Paris")
    assert schemas._default_tz() == "Europe/Paris"


def test_invalid_env_falls_through(monkeypatch):
    monkeypatch.setenv("SCHEDULER_TIMEZONE", "Not/ARealZone")
    result = schemas._default_tz()
    assert result != "Not/ARealZone"
    ZoneInfo(result)  # whatever it settled on is a real zone


# ── /etc/localtime resolver ──────────────────────────────────────────────

def test_localtime_parses_macos_path(monkeypatch):
    monkeypatch.setattr(schemas.os.path, "realpath",
                        lambda p: "/var/db/timezone/zoneinfo/America/Los_Angeles")
    assert schemas._system_tz_from_localtime() == "America/Los_Angeles"


def test_localtime_parses_linux_path(monkeypatch):
    monkeypatch.setattr(schemas.os.path, "realpath",
                        lambda p: "/usr/share/zoneinfo/America/New_York")
    assert schemas._system_tz_from_localtime() == "America/New_York"


def test_localtime_none_without_marker(monkeypatch):
    monkeypatch.setattr(schemas.os.path, "realpath", lambda p: "/etc/localtime")
    assert schemas._system_tz_from_localtime() is None


def test_localtime_none_for_bogus_zone(monkeypatch):
    monkeypatch.setattr(schemas.os.path, "realpath",
                        lambda p: "/usr/share/zoneinfo/Bogus/Zone")
    assert schemas._system_tz_from_localtime() is None


# ── integration: no env, no .key → localtime fallback (NOT UTC) ───────────

def test_falls_back_to_localtime_when_no_key(monkeypatch):
    monkeypatch.delenv("SCHEDULER_TIMEZONE", raising=False)
    _force_no_key(monkeypatch)
    monkeypatch.setattr(schemas, "_system_tz_from_localtime",
                        lambda: "America/Los_Angeles")
    assert schemas._default_tz() == "America/Los_Angeles"


def test_final_fallback_utc(monkeypatch):
    monkeypatch.delenv("SCHEDULER_TIMEZONE", raising=False)
    _force_no_key(monkeypatch)
    monkeypatch.setattr(schemas, "_system_tz_from_localtime", lambda: None)
    assert schemas._default_tz() == "UTC"
