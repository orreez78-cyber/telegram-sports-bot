"""Unit tests for caching and normalization utilities in bot.py."""
import time

from bot import TTLCache, _normalize_date, _normalize_name


class TestTTLCache:
    def test_set_and_get(self):
        cache = TTLCache(ttl_seconds=100, maxsize=10)
        cache.set("a", 123)
        assert cache.get("a") == 123

    def test_missing_key_returns_none(self):
        cache = TTLCache()
        assert cache.get("nope") is None

    def test_expired_entry_returns_none_and_is_evicted(self):
        cache = TTLCache(ttl_seconds=0, maxsize=10)
        cache.set("a", 1)
        # ttl of 0 means the entry is immediately stale.
        time.sleep(0.01)
        assert cache.get("a") is None
        assert "a" not in cache.cache

    def test_maxsize_evicts_oldest(self):
        cache = TTLCache(ttl_seconds=100, maxsize=2)
        cache.set("a", 1)
        time.sleep(0.01)
        cache.set("b", 2)
        time.sleep(0.01)
        cache.set("c", 3)  # should evict "a" (oldest)
        assert cache.get("a") is None
        assert cache.get("b") == 2
        assert cache.get("c") == 3
        assert len(cache.cache) == 2

    def test_overwrite_updates_value(self):
        cache = TTLCache(ttl_seconds=100)
        cache.set("k", 1)
        cache.set("k", 2)
        assert cache.get("k") == 2


class TestNormalizeDate:
    def test_empty_string(self):
        assert _normalize_date("") == ""

    def test_none_is_falsy(self):
        assert _normalize_date(None) == ""

    def test_iso_datetime_normalized(self):
        assert _normalize_date("2024-05-01T18:30:00Z") == "2024-05-01 18:30:00"

    def test_truncated_to_19_chars(self):
        assert _normalize_date("2024-05-01T18:30:00.123456Z") == "2024-05-01 18:30:00"


class TestNormalizeName:
    def test_lowercases_and_strips_non_alphanumeric(self):
        assert _normalize_name("Manchester United!") == "manchesterunited"

    def test_spaces_and_punctuation_removed(self):
        assert _normalize_name("Real  Madrid C.F.") == "realmadridcf"

    def test_digits_preserved(self):
        assert _normalize_name("Schalke 04") == "schalke04"

    def test_matches_regardless_of_formatting(self):
        assert _normalize_name("FC Bayern") == _normalize_name("fcbayern")
