"""Tests for the hardened HTTP retry helper in bot.py."""
import asyncio

import pytest

import bot

pytestmark = pytest.mark.asyncio


class _FakeResp:
    def __init__(self, status, payload=None):
        self.status = status
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self, content_type=None):
        return self._payload


class _FakeSession:
    """Returns queued responses; raising entries are raised as exceptions."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def get(self, url, headers=None, params=None):
        self.calls += 1
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def instant(_):
        return None
    monkeypatch.setattr(bot.asyncio, "sleep", instant)


async def _use_session(monkeypatch, responses):
    session = _FakeSession(responses)

    async def fake_get_session():
        return session

    monkeypatch.setattr(bot, "get_session", fake_get_session)
    return session


async def test_returns_payload_on_200(monkeypatch):
    session = await _use_session(monkeypatch, [_FakeResp(200, {"ok": 1})])
    result = await bot.fetch_json_with_retry("http://x")
    assert result == {"ok": 1}
    assert session.calls == 1


async def test_retries_on_500_then_succeeds(monkeypatch):
    session = await _use_session(monkeypatch, [_FakeResp(503), _FakeResp(200, [1, 2])])
    result = await bot.fetch_json_with_retry("http://x", max_retries=3)
    assert result == [1, 2]
    assert session.calls == 2


async def test_retries_on_429(monkeypatch):
    session = await _use_session(monkeypatch, [_FakeResp(429), _FakeResp(200, {"a": 1})])
    result = await bot.fetch_json_with_retry("http://x", max_retries=3)
    assert result == {"a": 1}


async def test_does_not_retry_client_error(monkeypatch):
    session = await _use_session(monkeypatch, [_FakeResp(404), _FakeResp(200, {"never": 1})])
    result = await bot.fetch_json_with_retry("http://x", max_retries=3)
    assert result is None
    assert session.calls == 1  # 404 is terminal


async def test_returns_none_after_exhausting_retries(monkeypatch):
    session = await _use_session(monkeypatch, [_FakeResp(500), _FakeResp(500), _FakeResp(500)])
    result = await bot.fetch_json_with_retry("http://x", max_retries=3)
    assert result is None
    assert session.calls == 3


async def test_recovers_from_transient_exception(monkeypatch):
    session = await _use_session(monkeypatch, [asyncio.TimeoutError(), _FakeResp(200, {"ok": True})])
    result = await bot.fetch_json_with_retry("http://x", max_retries=3)
    assert result == {"ok": True}
