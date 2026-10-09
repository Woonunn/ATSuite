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

    def _query(self, action, params, *, region=''):
        if self.client is None:
            from aliyunsdkcore.client import AcsClient
            self.client = AcsClient(os.environ['ALIBABA_CLOUD_ACCESS_KEY_ID'],os.environ['ALIBABA_CLOUD_ACCESS_KEY_SECRET'],region or os.environ.get('ALIBABA_CLOUD_REGION_ID','cn-hangzhou'))
        from aliyunsdkcore.request import CommonRequest
        request=CommonRequest()
        request.set_protocol_type('https');request.set_accept_format('json')
        request.set_domain('business.aliyuncs.com');request.set_method('POST')
        request.set_version('2017-12-14');request.set_action_name(action)
        for key,value in params.items():
            request.add_query_param(key,value)
        raw=self.client.do_action_with_exception(request)
        payload=json.loads(raw.decode() if isinstance(raw,bytes) else raw)
        if not payload.get('Success'):
            raise ValueError('aliyun_catalog_query_failed:'+str(payload.get('Code')))
        return payload

    def fetch_candidate(self, candidate, *, service_aliases):
        from dataclasses import replace
        get=lambda key,default=None:candidate.get(key,default) if isinstance(candidate,dict) else getattr(candidate,key,default)
        region=get('region','')
        rows=[];page=1
        while True:
            response=self._query('QueryProductList',{'PageNum':page,'PageSize':100,'SubscriptionType':'PayAsYouGo'},region=region)
            data=response.get('Data') or {}
            products=((data.get('ProductList') or {}).get('Product') or [])
            rows.extend(products)
            total=int(data.get('TotalCount') or len(rows))
            if len(rows)>=total or not products:
                break
            page+=1
        aliases={value.lower() for value in service_aliases}
        matched=[row for row in rows if str(row.get('ProductCode','')).lower() in aliases or str(row.get('ProductName','')).lower() in aliases]
        if len(matched)!=1:
            raise ValueError('ambiguous_or_missing_aliyun_price_service')
        product=matched[0]['ProductCode']
        description=self._query('DescribePricingModule',{'ProductCode':product,'SubscriptionType':'PayAsYouGo'},region=region)
        descriptions=((description.get('Data') or {}).get('ModuleList') or {}).get('Module') or []
        resources=get('resources_by_target',{}) or {'default':{}}
        cards=[]
        for target,resource in resources.items():
            values={'region':region,'regionid':region,'cpu':resource.get('cpu'),'vcpu':resource.get('cpu'),'memory':resource.get('memory_mb'),'memorysize':resource.get('memory_mb'),'architecture':get('runtime_architecture','')}
            modules=[]
            for module in descriptions:
                keys=(module.get('ConfigList') or {}).get('ConfigList') or []
                config=[]
                for key in keys:
                    value=values.get(str(key).lower())
                    if value is None or value=='':
                        raise ValueError('unsupported_aliyun_pricing_property:'+str(key))
                    config.append(str(key)+':'+str(value))
                modules.append({'ModuleCode':module['ModuleCode'],'PriceType':module.get('PriceType','Usage'),'Config':','.join(config)})
            if not modules:
                raise ValueError('missing_aliyun_pricing_modules')
            for card in self.fetch(product,modules,region=region):
                cards.append(replace(card,metadata={**card.metadata,'target':target,'module_configs':modules},snapshot_id='rate-'+digest({'card':card.snapshot_id,'modules':modules},length=24)))
        return cards

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
            price_field = "UnitPrice"
            if detail.get(price_field) is None:
                raise ValueError("missing_aliyun_unit_price")
            price = Decimal(str(detail[price_field]))
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
