from __future__ import annotations

import json
import os
from decimal import Decimal
from typing import Any, Iterable

from atsuite.market_data.model import PriceTier, RateCard
from atsuite.recommender.contracts import utc_now
from atsuite.recommender.identity import digest


class AlibabaPriceAdapter:
    """Alibaba BSS GetPayAsYouGoPrice adapter using the existing core SDK."""

    name = "aliyun_bss_get_pay_as_you_go_price"

    def __init__(self, client=None):
        self.client = client

    def fetch(self, product_code: str, modules: Iterable[dict[str, str]], *, region: str = "") -> list[RateCard]:
        if self.client is None:
            from aliyunsdkcore.client import AcsClient
            self.client = AcsClient(
                os.environ["ALIBABA_CLOUD_ACCESS_KEY_ID"],
                os.environ["ALIBABA_CLOUD_ACCESS_KEY_SECRET"],
                region or os.environ.get("ALIBABA_CLOUD_REGION_ID", "cn-hangzhou"),
            )
        from aliyunsdkcore.request import CommonRequest
        request = CommonRequest()
        request.set_protocol_type("https")
        request.set_accept_format("json")
        request.set_domain("business.aliyuncs.com")
        request.set_method("POST")
        request.set_version("2017-12-14")
        request.set_action_name("GetPayAsYouGoPrice")
        request.add_query_param("ProductCode", product_code)
        request.add_query_param("SubscriptionType", "PayAsYouGo")
        if region:
            request.add_query_param("Region", region)
        module_list = list(modules)
        for index, module in enumerate(module_list, 1):
            for key in ("ModuleCode", "PriceType", "Config"):
                request.add_query_param(f"ModuleList.{index}.{key}", module[key])
        raw = self.client.do_action_with_exception(request)
        payload = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        return self.parse(payload, product_code=product_code, region=region, modules=module_list)

    def parse(self, payload: dict[str, Any], *, product_code: str, region: str = "", modules: Iterable[dict[str, str]] = ()) -> list[RateCard]:
        if not payload.get("Success") or payload.get("Code") not in (None, "Success"):
            raise ValueError(f"Alibaba pricing failed: {payload.get('Code')}: {payload.get('Message')}")
        data = payload.get("Data") or {}
        currency = str(data.get("Currency") or "CNY").upper()
        details = ((data.get("ModuleDetails") or {}).get("ModuleDetail") or [])
        module_types = {str(item.get("ModuleCode")): str(item.get("PriceType") or "Usage") for item in modules}
        unit_map = {"Hour": "hour", "Usage": "unit", "Month": "month", "Year": "year"}
        cards = []
        for detail in details:
            module_code = str(detail.get("ModuleCode") or "")
            price_field = "UnitPrice" if detail.get("UnitPrice") not in (None, 0, "0") else "OriginalCost"
            price = Decimal(str(detail.get(price_field, 0)))
            price_type = module_types.get(module_code, "Usage")
            row = {"product": product_code, "module": module_code, "region": region, "currency": currency,
                   "price": str(price), "request": payload.get("RequestId")}
            cards.append(RateCard(
                "rate-" + digest(row, length=24), "ali", product_code, module_code, region or "default", "pay_as_you_go",
                unit_map.get(price_type, price_type.lower()), currency, (PriceTier(None, price),), utc_now(), utc_now(), self.name,
                "fresh", {"price_field": price_field, "invoice_discount": detail.get("InvoiceDiscount"),
                           "cost_after_discount": detail.get("CostAfterDiscount"), "request_id": payload.get("RequestId")},
            ))
        return cards
