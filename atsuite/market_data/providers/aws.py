from __future__ import annotations

import json
from decimal import Decimal
from typing import Any, Iterable

from atsuite.market_data.model import PriceTier, RateCard
from atsuite.recommender.contracts import utc_now
from atsuite.recommender.identity import digest


class AWSPriceAdapter:
    """AWS Price List Query adapter using GetProducts and its paginator."""

    name = "aws_price_list_query"

    def __init__(self, client=None):
        if client is None:
            import boto3
            client = boto3.client("pricing", region_name="us-east-1")
        self.client = client

    def fetch(self, service_code: str, filters: Iterable[dict[str, str]], *, region: str = "") -> list[RateCard]:
        pages = self.client.get_paginator("get_products").paginate(
            ServiceCode=service_code,
            Filters=list(filters),
            FormatVersion="aws_v1",
        )
        cards = []
        for page in pages:
            for raw in page.get("PriceList", []):
                payload = json.loads(raw) if isinstance(raw, str) else raw
                cards.extend(self.parse(payload, region=region))
        return cards

    def parse(self, payload: dict[str, Any], *, region: str = "") -> list[RateCard]:
        product = payload.get("product") or {}
        sku = str(product.get("sku") or "")
        if not sku:
            return []
        attributes = product.get("attributes") or {}
        actual_region = str(attributes.get("regionCode") or region or "global")
        if region and actual_region not in {region, "global"}:
            return []
        cards = []
        for term_code, term in (payload.get("terms") or {}).get("OnDemand", {}).items():
            effective = str(term.get("effectiveDate") or payload.get("publicationDate") or utc_now())
            grouped: dict[tuple[str, str], list[tuple[Decimal, Decimal | None, Decimal, str, str]]] = {}
            for rate_code, dimension in (term.get("priceDimensions") or {}).items():
                begin = Decimal(str(dimension.get("beginRange") or 0))
                end = str(dimension.get("endRange", "Inf"))
                upper = None if end.lower() == "inf" else Decimal(end)
                unit = str(dimension.get("unit") or "unit")
                for currency, price in (dimension.get("pricePerUnit") or {}).items():
                    grouped.setdefault((unit, str(currency).upper()), []).append(
                        (begin, upper, Decimal(str(price)), str(rate_code), str(dimension.get("description") or ""))
                    )
            for (unit, currency), dimensions in grouped.items():
                dimensions.sort(key=lambda item: item[0])
                tiers = tuple(PriceTier(upper, price) for _, upper, price, _, _ in dimensions)
                rate_codes = [item[3] for item in dimensions]
                data = {"sku": sku, "term": term_code, "rate_codes": rate_codes, "region": actual_region,
                        "unit": unit, "currency": currency, "effective": effective, "tiers": [(str(t.up_to), str(t.unit_price)) for t in tiers], "source": self.name, "version": "1", "attributes": attributes}
                observed = utc_now()
                cards.append(RateCard(
                    "rate-" + digest(data, length=24), "aws", str(product.get("productFamily") or payload.get("serviceCode") or ""),
                    f"{sku}:{term_code}", actual_region, "on_demand", unit, currency, tiers,
                    fetched_at=observed, observed_at=observed, source=self.name, freshness="fresh",
                    metadata={"descriptions": [item[4] for item in dimensions], "attributes": attributes, "rate_codes": rate_codes},
                    effective_from=effective,
                ))
        return cards
