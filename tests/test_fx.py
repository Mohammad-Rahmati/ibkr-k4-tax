"""
tests/test_fx.py
----------------
Unit tests for fx.py — FX rate fetching and caching.

API backend: Sveriges Riksbank SWEA
  GET https://api.riksbank.se/swea/v1/Observations/{series}/{from}/{to}
  Response: [{"date": "2025-03-15", "value": 10.5}, …]
"""

import json
import time
from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from fx import FXRateProvider, _cache_key, _is_cache_fresh, _load_cache, _save_cache


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_provider(tmp_path: Path, **kwargs) -> FXRateProvider:
    """Create a FXRateProvider using a tmp cache file."""
    cache_file = tmp_path / "fx_cache.json"
    return FXRateProvider(cache_file=cache_file, **kwargs)


def _mock_api_response(rate: float = 10.5, target_date: date | None = None) -> MagicMock:
    """
    Return a mock requests.Response with a Riksbank-style payload.

    The response is a JSON list of {date, value} dicts.
    If *target_date* is None we use 2025-03-15 as the single entry.
    """
    if target_date is None:
        target_date = date(2025, 3, 15)
    mock_resp = MagicMock()
    mock_resp.raise_for_status.return_value = None
    mock_resp.status_code = 200
    mock_resp.headers = {}
    mock_resp.json.return_value = [
        {"date": target_date.isoformat(), "value": rate},
    ]
    return mock_resp


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

class TestCacheKey:
    def test_format(self):
        key = _cache_key(date(2025, 3, 15), "USD", "SEK")
        assert key == "2025-03-15:USD:SEK"

    def test_uppercase(self):
        key = _cache_key(date(2025, 3, 15), "usd", "sek")
        assert key == "2025-03-15:USD:SEK"


class TestIsCacheFresh:
    def test_fresh_entry(self):
        entry = {"rate": 10.5, "cached_at": time.time()}
        assert _is_cache_fresh(entry) is True

    def test_expired_entry(self):
        # cached 100 days ago
        entry = {"rate": 10.5, "cached_at": time.time() - 100 * 86400}
        assert _is_cache_fresh(entry) is False

    def test_missing_cached_at(self):
        entry = {"rate": 10.5}
        assert _is_cache_fresh(entry) is False


class TestLoadSaveCache:
    def test_round_trip(self, tmp_path):
        cache_file = tmp_path / "fx_cache.json"
        cache = {"version": 2, "rates": {"k": {"rate": 9.9, "cached_at": 0}}}
        _save_cache(cache, cache_file)
        loaded = _load_cache(cache_file)
        assert loaded["rates"]["k"]["rate"] == 9.9

    def test_missing_file_returns_empty(self, tmp_path):
        cache = _load_cache(tmp_path / "nonexistent.json")
        assert cache == {"version": 2, "rates": {}}

    def test_corrupt_file_returns_empty(self, tmp_path):
        bad_file = tmp_path / "bad.json"
        bad_file.write_text("NOT JSON", encoding="utf-8")
        cache = _load_cache(bad_file)
        assert cache == {"version": 2, "rates": {}}


# ---------------------------------------------------------------------------
# FXRateProvider.get_rate
# ---------------------------------------------------------------------------

class TestFXRateProviderGetRate:
    def test_same_currency_returns_one(self, tmp_path):
        provider = _make_provider(tmp_path)
        assert provider.get_rate(date(2025, 1, 1), base="SEK", target="SEK") == 1.0

    def test_api_called_on_cache_miss(self, tmp_path):
        provider = _make_provider(tmp_path)
        mock_resp = _mock_api_response(10.5, date(2025, 3, 15))
        with patch("fx.requests.Session") as mock_session_cls:
            mock_session = MagicMock()
            mock_session.get.return_value = mock_resp
            mock_session_cls.return_value = mock_session
            rate = provider.get_rate(date(2025, 3, 15), base="USD", target="SEK")
        assert rate == 10.5
        mock_session.get.assert_called_once()

    def test_cache_populated_after_api_call(self, tmp_path):
        provider = _make_provider(tmp_path)
        mock_resp = _mock_api_response(10.5, date(2025, 3, 15))
        with patch("fx.requests.Session") as mock_session_cls:
            mock_session = MagicMock()
            mock_session.get.return_value = mock_resp
            mock_session_cls.return_value = mock_session
            provider.get_rate(date(2025, 3, 15), base="USD", target="SEK")

        # Second call should NOT hit the API
        with patch("fx.requests.Session") as mock_session_cls2:
            mock_session2 = MagicMock()
            mock_session_cls2.return_value = mock_session2
            rate = provider.get_rate(date(2025, 3, 15), base="USD", target="SEK")
        mock_session2.get.assert_not_called()
        assert rate == 10.5

    def test_cache_persisted_to_disk(self, tmp_path):
        cache_file = tmp_path / "fx_cache.json"
        provider = FXRateProvider(cache_file=cache_file)
        mock_resp = _mock_api_response(10.5, date(2025, 3, 15))
        with patch("fx.requests.Session") as mock_session_cls:
            mock_session = MagicMock()
            mock_session.get.return_value = mock_resp
            mock_session_cls.return_value = mock_session
            provider.get_rate(date(2025, 3, 15), base="USD", target="SEK")

        assert cache_file.exists()
        data = json.loads(cache_file.read_text())
        assert "2025-03-15:USD:SEK" in data["rates"]

    def test_api_error_raises_when_no_cache(self, tmp_path):
        provider = _make_provider(tmp_path)
        with patch("fx.requests.Session") as mock_session_cls:
            mock_session = MagicMock()
            mock_session.get.side_effect = Exception("network error")
            mock_session_cls.return_value = mock_session
            with pytest.raises(RuntimeError):
                provider.get_rate(date(2025, 3, 15), base="USD", target="SEK")

    def test_stale_cache_used_on_api_error(self, tmp_path):
        cache_file = tmp_path / "fx_cache.json"
        # Pre-populate with a stale (expired) entry
        stale_cache = {
            "version": 2,
            "rates": {
                "2025-03-15:USD:SEK": {
                    "rate": 9.99,
                    "cached_at": time.time() - 200 * 86400,  # very old
                }
            },
        }
        _save_cache(stale_cache, cache_file)

        provider = FXRateProvider(cache_file=cache_file)
        with patch("fx.requests.Session") as mock_session_cls:
            mock_session = MagicMock()
            mock_session.get.side_effect = Exception("network error")
            mock_session_cls.return_value = mock_session
            rate = provider.get_rate(date(2025, 3, 15), base="USD", target="SEK")

        assert rate == 9.99

    def test_datetime_input_accepted(self, tmp_path):
        provider = _make_provider(tmp_path)
        mock_resp = _mock_api_response(10.5, date(2025, 3, 15))
        with patch("fx.requests.Session") as mock_session_cls:
            mock_session = MagicMock()
            mock_session.get.return_value = mock_resp
            mock_session_cls.return_value = mock_session
            rate = provider.get_rate(datetime(2025, 3, 15, 10, 30), base="USD", target="SEK")
        assert rate == 10.5


# ---------------------------------------------------------------------------
# FXRateProvider.prefetch_rates
# ---------------------------------------------------------------------------

class TestPrefetchRates:
    def _make_multi_date_response(self, dates_rates: dict) -> MagicMock:
        """Build a Riksbank-style response covering multiple dates."""
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.status_code = 200
        mock_resp.headers = {}
        mock_resp.json.return_value = [
            {"date": d.isoformat(), "value": v}
            for d, v in dates_rates.items()
        ]
        return mock_resp

    def test_prefetch_multiple_dates(self, tmp_path):
        provider = _make_provider(tmp_path)
        dates = [date(2025, 1, 10), date(2025, 2, 20), date(2025, 3, 30)]
        mock_resp = self._make_multi_date_response({d: 10.0 for d in dates})
        with patch("fx.requests.Session") as mock_session_cls:
            mock_session = MagicMock()
            mock_session.get.return_value = mock_resp
            mock_session_cls.return_value = mock_session
            result = provider.prefetch_rates(dates, base="USD", target="SEK")
        assert len(result) == 3
        for d in dates:
            assert d in result
            assert result[d] == 10.0

    def test_prefetch_deduplicates_dates(self, tmp_path):
        provider = _make_provider(tmp_path)
        dates = [date(2025, 1, 10), date(2025, 1, 10), date(2025, 1, 10)]
        mock_resp = self._make_multi_date_response({date(2025, 1, 10): 10.0})
        with patch("fx.requests.Session") as mock_session_cls:
            mock_session = MagicMock()
            mock_session.get.return_value = mock_resp
            mock_session_cls.return_value = mock_session
            provider.prefetch_rates(dates, base="USD", target="SEK")
        # Should only make one API call despite duplicate dates
        assert mock_session.get.call_count == 1
