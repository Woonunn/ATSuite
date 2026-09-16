from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from decimal import Decimal
from typing import Any

from atsuite.market_data.model import PriceTier, RateCard
from atsuite.recommender.contracts import utc_now
from atsuite.recommender.identity import digest


class GCPCatalogAdapter:
    name = "gcp_cloud_billing_catalog"
    base_url = "https://cloudbilling.googleapis.com/v1"

    def __init__(self, api_key: str | None = None, opener=None):
        self.api_key = api_key or os.environ.get("GOOGLE_API_KEY", "")
        self.opener = opener or urllib.request.urlopen

    def fetch(self, service_id: str, *, region: str = "", currency: str = "USD") -> list[RateCard]:
        if not self.api_key:
            raise ValueError("GOOGLE_API_KEY is required for the public catalog API")
        token = ""
        cards = []
        while True:
            query = {"key": self.api_key, "currencyCode": currency, "pageSize": 5000}
            if token:
                query["pageToken"] = token
            url = f"{self.base_url}/services/{urllib.parse.quote(service_id, safe='')}/skus?{urllib.parse.urlencode(query)}"
            with self.opener(urllib.request.Request(url, headers={"User-Agent": "ATSuite/1"}), timeout=15) as response:
                payload = json.loads(response.read())
            for sku in payload.get("skus", []):
                cards.extend(self.parse(sku, region=region, currency=currency))
            token = str(payload.get("nextPageToken") or "")
            if not token:
                return cards

    @staticmethod
    def _money(payload: dict[str, Any]) -> Decimal:
        return Decimal(str(payload.get("units", 0))) + Decimal(str(payload.get("nanos", 0))) / Decimal("1000000000")

    def parse(self, sku: dict[str, Any], *, region: str = "", currency: str = "USD") -> list[RateCard]:
        regions = [str(value) for value in sku.get("serviceRegions", [])]
        if region and regions and region not in regions:
            return []
        pricing = sorted(sku.get("pricingInfo", []), key=lambda item: str(item.get("effectiveTime", "")))
        if not pricing:
            return []
        latest = pricing[-1]
        expression = latest.get("pricingExpression") or {}
        raw_tiers = sorted(expression.get("tieredRates", []), key=lambda item: Decimal(str(item.get("startUsageAmount", 0))))
        tiers = []
        for index, raw in enumerate(raw_tiers):
            upper = Decimal(str(raw_tiers[index + 1].get("startUsageAmount"))) if index + 1 < len(raw_tiers) else None
            tiers.append(PriceTier(upper, self._money(raw.get("unitPrice") or {})))
        if not tiers:
            return []
        category = sku.get("category") or {}
        actual_region = region or (regions[0] if len(regions) == 1 else "multi-region")
        data = {"sku": sku.get("skuId"), "region": actual_region, "currency": currency, "effective": latest.get("effectiveTime")}
        return [RateCard(
            "rate-" + digest(data, length=24), "gcp", str(category.get("serviceDisplayName") or ""), str(sku.get("skuId") or ""),
            actual_region, str(category.get("usageType") or "on_demand"), str(expression.get("usageUnit") or "unit"), currency.upper(),
            tuple(tiers), utc_now(), str(latest.get("effectiveTime") or utc_now()), self.name, "fresh",
            {"description": sku.get("description", ""), "regions": regions, "base_unit": expression.get("baseUnit"),
             "base_unit_conversion_factor": expression.get("baseUnitConversionFactor"), "display_quantity": expression.get("displayQuantity")},
        )]
