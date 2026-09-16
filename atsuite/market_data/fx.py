from __future__ import annotations

import json
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from atsuite.recommender.identity import digest
from atsuite.recommender.contracts import utc_now
from atsuite.market_data.model import FxSnapshot


class FxProvider:
    name = "manual"

    def fetch(self, base_currency: str = "EUR") -> FxSnapshot:
        raise NotImplementedError


class StaticFxProvider(FxProvider):
    name = "manual"

    def __init__(self, rates: dict[str, float | str], *, observed_at: str = "", fetched_at: str = ""):
        self.rates = rates
        self.observed_at = observed_at or utc_now()
        self.fetched_at = fetched_at or utc_now()

    def fetch(self, base_currency: str = "USD") -> FxSnapshot:
        base = base_currency.upper()
        rates = {str(key).upper(): str(value) for key, value in self.rates.items()}
        rates.pop(base, None)
        return FxSnapshot("fx-" + digest({"base": base, "rates": rates, "observed_at": self.observed_at}), base, rates,
                          self.observed_at, self.fetched_at, self.name)


class ECBFxProvider(FxProvider):
    name = "ecb"
    url = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"

    def __init__(self, opener=None):
        self.opener = opener or urllib.request.urlopen

    def fetch(self, base_currency: str = "EUR") -> FxSnapshot:
        request = urllib.request.Request(self.url, headers={"User-Agent": "ATSuite/1"})
        with self.opener(request, timeout=10) as response:
            body = response.read()
        text = body.decode("utf-8")
        # XML parsing is intentionally small and stdlib only.
        import re
        rates = {code.upper(): value for code, value in re.findall(r'currency="([A-Z]{3})"[^>]*rate="([0-9.]+)"', text)}
        if not rates:
            raise ValueError("ECB response contained no rates")
        rates["EUR"] = "1"
        return convert_base(FxSnapshot("fx-" + digest(rates), "EUR", rates, utc_now(), utc_now(), self.name, self.url), base_currency)


def convert_base(snapshot: FxSnapshot, base_currency: str) -> FxSnapshot:
    base = base_currency.upper()
    if base == snapshot.base_currency:
        return snapshot
    base_to_old = snapshot.rate(base)
    rates = {currency: str(snapshot.rate(currency) / base_to_old) for currency in snapshot.rates if currency != base}
    rates[snapshot.base_currency] = str(Decimal("1") / base_to_old)
    return FxSnapshot("fx-" + digest({"base": base, "rates": rates, "observed_at": snapshot.observed_at}), base, rates,
                      snapshot.observed_at, snapshot.fetched_at, snapshot.source, snapshot.source_url, snapshot.freshness)


def load_fx_snapshot(path: str | Path) -> FxSnapshot:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return FxSnapshot(**payload)
