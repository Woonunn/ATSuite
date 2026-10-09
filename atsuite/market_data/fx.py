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
        # Reject entity declarations before using stdlib's non-network XML parser.
        if b'<!DOCTYPE' in body.upper() or b'<!ENTITY' in body.upper():
            raise ValueError('ECB XML entities are forbidden')
        from xml.etree import ElementTree
        root = ElementTree.fromstring(body)
        dated = [node for node in root.iter() if node.attrib.get('time')]
        if len(dated) != 1:
            raise ValueError('ECB response requires one source date')
        observed = dated[0].attrib['time']
        datetime.strptime(observed, '%Y-%m-%d')
        rates = {node.attrib['currency'].upper(): str(Decimal(node.attrib['rate'])) for node in dated[0].iter() if 'currency' in node.attrib and 'rate' in node.attrib}
        if not rates:
            raise ValueError('ECB response contained no rates')
        rates['EUR'] = '1'
        content = {'base': 'EUR', 'rates': rates, 'observed_at': observed, 'source': self.name, 'source_url': self.url, 'source_version': '1'}
        snapshot = FxSnapshot('fx-' + digest(content), 'EUR', rates, observed, utc_now(), self.name, self.url,
                              observed_precision='date', effective_at=observed)
        return convert_base(snapshot, base_currency)


def convert_base(snapshot: FxSnapshot, base_currency: str) -> FxSnapshot:
    base = base_currency.upper()
    if base == snapshot.base_currency:
        return snapshot
    base_to_old = snapshot.rate(base)
    rates = {currency: str(snapshot.rate(currency) / base_to_old) for currency in snapshot.rates if currency != base}
    rates[snapshot.base_currency] = str(Decimal("1") / base_to_old)
    return FxSnapshot("fx-" + digest({"base": base, "rates": rates, "observed_at": snapshot.observed_at}), base, rates,
                      snapshot.observed_at, snapshot.fetched_at, snapshot.source, snapshot.source_url, snapshot.freshness,
                      snapshot.observed_precision, snapshot.effective_at, snapshot.source_version)


def load_fx_snapshot(path: str | Path) -> FxSnapshot:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return FxSnapshot(**payload)
