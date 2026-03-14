"""
fx.py
-----
Fetch historical foreign-exchange rates from Sveriges Riksbank SWEA API
and cache them locally.

API used
--------
Sveriges Riksbank SWEA (Swedish Economic Analysis) — free, no API key needed.

    GET https://api.riksbank.se/swea/v1/Observations/{series}/{from}/{to}

Series naming convention:  SEK{CURRENCY}PMI
    e.g. SEKUSDPMI = number of SEK per 1 USD

The endpoint returns only business days.  When a trade falls on a weekend or
public holiday the closest preceding business day's rate is used instead.

Caching
-------
Rates are stored in a local JSON file so the API is not called on every run.
Historical rates are immutable, so cached entries are treated as permanently
valid unless FX_CACHE_TTL_DAYS is set to 0.
"""

import json
import logging
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config import (
    FX_API_BASE_URL,
    FX_RIKSBANK_SERIES,
    FX_CACHE_FILE,
    FX_CACHE_TTL_DAYS,
    FX_FALLBACK_RATE,
    TARGET_CURRENCY,
)

logger = logging.getLogger(__name__)

_CACHE_VERSION = 2


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _load_cache(cache_file: Path) -> dict:
    """Load the on-disk FX cache, returning an empty structure on failure."""
    if not cache_file.exists():
        return {"version": _CACHE_VERSION, "rates": {}}
    try:
        with cache_file.open(encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict) or "rates" not in data:
            logger.warning("FX cache file has unexpected format; resetting.")
            return {"version": _CACHE_VERSION, "rates": {}}
        return data
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not read FX cache (%s); starting fresh.", exc)
        return {"version": _CACHE_VERSION, "rates": {}}


def _save_cache(cache: dict, cache_file: Path) -> None:
    """Persist the FX cache to disk."""
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        with cache_file.open("w", encoding="utf-8") as fh:
            json.dump(cache, fh, indent=2)
    except OSError as exc:
        logger.warning("Could not write FX cache: %s", exc)


def _cache_key(trade_date: date, base: str, target: str) -> str:
    return f"{trade_date.isoformat()}:{base.upper()}:{target.upper()}"


def _is_cache_fresh(entry: dict) -> bool:
    """Return True if the cached entry is within the configured TTL."""
    if FX_CACHE_TTL_DAYS == 0:
        return False
    cached_at = entry.get("cached_at", 0)
    age_days = (time.time() - cached_at) / 86400
    return age_days < FX_CACHE_TTL_DAYS


# ---------------------------------------------------------------------------
# Riksbank API helpers
# ---------------------------------------------------------------------------

def _riksbank_series(base: str, target: str) -> str | None:
    """
    Return the Riksbank series name for a base→SEK conversion, or None if
    the currency pair is not supported.
    """
    if target.upper() != "SEK":
        return None
    return FX_RIKSBANK_SERIES.get(base.upper())


def _fetch_range_from_riksbank(
    series: str,
    from_date: date,
    to_date: date,
) -> dict[date, float]:
    """
    Fetch a range of historical rates from the Riksbank SWEA API.

    Returns a dict mapping date → rate (SEK per 1 unit of base currency).
    Only business days are included in the response.
    """
    url = f"{FX_API_BASE_URL}/Observations/{series}/{from_date.isoformat()}/{to_date.isoformat()}"
    logger.debug("Riksbank API request: %s", url)

    # Retry up to 5 times on 429/5xx with exponential back-off.
    session = requests.Session()
    retry = Retry(
        total=5,
        backoff_factor=2,         # waits 2, 4, 8, 16, 32 seconds between retries
        status_forcelist=[429, 500, 502, 503, 504],
        raise_on_status=False,
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))

    try:
        response = session.get(url, timeout=30)
        if response.status_code == 429:
            retry_after = int(response.headers.get("Retry-After", 60))
            logger.warning("Riksbank rate-limited; sleeping %ds …", retry_after)
            time.sleep(retry_after)
            response = session.get(url, timeout=30)
        response.raise_for_status()
    except (requests.RequestException, Exception) as exc:
        raise RuntimeError(f"Riksbank API request failed for {series}: {exc}") from exc

    payload = response.json()
    if not isinstance(payload, list):
        raise RuntimeError(f"Unexpected Riksbank response for {series}: {payload}")

    result: dict[date, float] = {}
    for entry in payload:
        try:
            d = date.fromisoformat(entry["date"])
            v = float(entry["value"])
            result[d] = v
        except (KeyError, ValueError, TypeError) as exc:
            logger.warning("Skipping bad Riksbank entry %s: %s", entry, exc)

    return result


def _nearest_preceding_rate(
    target_date: date, rates: dict[date, float]
) -> float | None:
    """
    Return the rate for *target_date* or the closest preceding date in *rates*.

    Used to handle weekends and public holidays — looks back up to 10 days.
    """
    if not rates:
        return None
    d = target_date
    for _ in range(10):
        if d in rates:
            return rates[d]
        d -= timedelta(days=1)
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class FXRateProvider:
    """
    Provides historical FX rates (→ SEK) with local disk caching.

    Uses the Sveriges Riksbank SWEA API — free, no key required.

    Parameters
    ----------
    cache_file:
        Path to the JSON cache file.
    base_currency:
        Default source currency (e.g. ``"USD"``).
    target_currency:
        Default target currency (must be ``"SEK"``).
    """

    def __init__(
        self,
        cache_file: Path | None = None,
        base_currency: str = "USD",
        target_currency: str = TARGET_CURRENCY,
    ) -> None:
        self._cache_file = Path(cache_file) if cache_file else FX_CACHE_FILE
        self._base_currency = base_currency.upper()
        self._target_currency = target_currency.upper()
        self._cache = _load_cache(self._cache_file)

    def get_rate(
        self,
        trade_date: date | datetime,
        base: str | None = None,
        target: str | None = None,
    ) -> float:
        """
        Return the exchange rate for *base* → *target* on *trade_date*.

        Falls back to the closest preceding business day if no rate exists
        for the exact date (weekend / public holiday).

        Parameters
        ----------
        trade_date:
            Date of the trade.
        base:
            Source currency (defaults to instance ``base_currency``).
        target:
            Target currency (defaults to ``"SEK"``).

        Returns
        -------
        float
        """
        if isinstance(trade_date, datetime):
            trade_date = trade_date.date()

        base = (base or self._base_currency).upper()
        target = (target or self._target_currency).upper()

        if base == target:
            return 1.0

        key = _cache_key(trade_date, base, target)
        cached = self._cache["rates"].get(key)
        if cached and _is_cache_fresh(cached):
            logger.debug("FX cache hit: %s = %.6f", key, cached["rate"])
            return float(cached["rate"])

        try:
            rate = self._fetch_and_cache_single(trade_date, base, target)
            return rate
        except (requests.RequestException, RuntimeError) as exc:
            logger.error("FX API error for %s %s/%s: %s", trade_date, base, target, exc)

            if cached:
                logger.warning(
                    "Using stale cached rate %.6f for %s %s/%s",
                    cached["rate"], trade_date, base, target,
                )
                return float(cached["rate"])

            if FX_FALLBACK_RATE is not None:
                logger.warning(
                    "Using fallback rate %.6f for %s %s/%s",
                    FX_FALLBACK_RATE, trade_date, base, target,
                )
                return FX_FALLBACK_RATE

            raise RuntimeError(
                f"Cannot obtain FX rate for {trade_date} {base}/{target}: {exc}"
            ) from exc

    def _fetch_and_cache_single(
        self, trade_date: date, base: str, target: str
    ) -> float:
        """Fetch one date's rate from Riksbank, handling non-business-days."""
        series = _riksbank_series(base, target)
        if series is None:
            raise RuntimeError(
                f"Riksbank SWEA does not support {base}/{target}. "
                f"Add the series to FX_RIKSBANK_SERIES in config.py."
            )

        # Fetch a 14-day window to handle long holiday stretches.
        window_start = trade_date - timedelta(days=14)
        rates = _fetch_range_from_riksbank(series, window_start, trade_date)

        rate = _nearest_preceding_rate(trade_date, rates)
        if rate is None:
            raise RuntimeError(
                f"No Riksbank rate found for {series} on or before {trade_date}."
            )

        actual_date = max((d for d in rates if d <= trade_date), default=trade_date)
        if actual_date != trade_date:
            logger.info(
                "No Riksbank rate for %s (%s/%s); using %s (%.6f).",
                trade_date, base, target, actual_date, rate,
            )
        else:
            logger.info(
                "Fetched Riksbank rate %s %s/%s = %.6f", trade_date, base, target, rate
            )

        key = _cache_key(trade_date, base, target)
        self._cache["rates"][key] = {"rate": rate, "cached_at": time.time()}
        _save_cache(self._cache, self._cache_file)
        return rate

    def prefetch_rates(
        self,
        dates: list[date | datetime],
        base: str | None = None,
        target: str | None = None,
    ) -> dict[date, float]:
        """
        Fetch rates for multiple dates efficiently using a single API range call.

        Parameters
        ----------
        dates:
            Dates to fetch.
        base:
            Source currency override.
        target:
            Target currency override.

        Returns
        -------
        dict mapping ``date`` → rate.
        """
        base = (base or self._base_currency).upper()
        target = (target or self._target_currency).upper()

        if base == target:
            return {(d.date() if isinstance(d, datetime) else d): 1.0 for d in dates}

        unique_dates = sorted(
            {(d.date() if isinstance(d, datetime) else d) for d in dates}
        )

        missing: list[date] = []
        result: dict[date, float] = {}
        for d in unique_dates:
            cached = self._cache["rates"].get(_cache_key(d, base, target))
            if cached and _is_cache_fresh(cached):
                result[d] = float(cached["rate"])
            else:
                missing.append(d)

        if not missing:
            return result

        series = _riksbank_series(base, target)
        if series is None:
            for d in missing:
                result[d] = self.get_rate(d, base=base, target=target)
            return result

        try:
            from_date = min(missing) - timedelta(days=14)
            to_date = max(missing)
            api_rates = _fetch_range_from_riksbank(series, from_date, to_date)

            for d in missing:
                rate = _nearest_preceding_rate(d, api_rates)
                if rate is None:
                    logger.warning("No Riksbank rate for %s %s/%s; skipping.", d, base, target)
                    continue
                result[d] = rate
                self._cache["rates"][_cache_key(d, base, target)] = {
                    "rate": rate, "cached_at": time.time()
                }

            _save_cache(self._cache, self._cache_file)
            logger.info("Pre-fetched %d Riksbank rates for %s/%s.", len(missing), base, target)

        except (requests.RequestException, RuntimeError) as exc:
            logger.error("Riksbank bulk fetch failed for %s/%s: %s", base, target, exc)
            for d in missing:
                try:
                    result[d] = self.get_rate(d, base=base, target=target)
                except RuntimeError:
                    pass

        return result
