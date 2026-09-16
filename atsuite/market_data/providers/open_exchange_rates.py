from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from atsuite.market_data.model import FxSnapshot
from atsuite.recommender.contracts import utc_now
from atsuite.recommender.identity import digest


class OpenExchangeRatesProvider:
    name = "open_exchange_rates"
    url = "https://openexchangerates.org/api/latest.json"

    def __init__(self, app_id: str | None = None, opener=None):
        self.app_id = app_id or os.environ.get("OPEN_EXCHANGE_RATES_APP_ID", "")
        self.opener = opener or urllib.request.urlopen

    def fetch(self, base_currency: str = "USD") -> FxSnapshot:
        if not self.app_id:
            raise ValueError("OPEN_EXCHANGE_RATES_APP_ID is required")
        url = f"{self.url}?{urllib.parse.urlencode({'app_id': self.app_id, 'base': base_currency.upper()})}"
        with self.opener(urllib.request.Request(url, headers={"User-Agent": "ATSuite/1"}), timeout=10) as response:
            payload = json.loads(response.read())
        rates = {str(code).upper(): str(value) for code, value in (payload.get("rates") or {}).items()}
        if not rates:
            raise ValueError("Open Exchange Rates response contained no rates")
        timestamp = int(payload.get("timestamp") or 0)
        observed = datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z") if timestamp else utc_now()
        base = str(payload.get("base") or base_currency).upper()
        return FxSnapshot("fx-" + digest({"base": base, "rates": rates, "observed": observed}, length=24), base, rates,
                          observed, utc_now(), self.name, self.url)
