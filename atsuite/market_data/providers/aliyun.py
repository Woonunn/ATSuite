from __future__ import annotations

import json
import os
import re
import urllib.request
from decimal import Decimal
from html.parser import HTMLParser
from typing import Any, Iterable

from atsuite.market_data.model import PriceTier, RateCard
from atsuite.recommender.contracts import utc_now
from atsuite.recommender.identity import digest


class AlibabaPriceAdapter:
    """BSS prices, with live official FC tariffs when BSS has no modules."""

    name = "aliyun_bss_get_pay_as_you_go_price"

    def __init__(self, client=None, *, opener=None):
        self.client = client
        self.opener = opener or urllib.request.urlopen

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
            response=self._query('QueryProductList',{'PageNum':page,'PageSize':100,'QueryTotalCount':'true'},region=region)
            data=response.get('Data') or {}
            products=((data.get('ProductList') or {}).get('Product') or [])
            rows.extend(products)
            total=int(data.get('TotalCount') or 0)
            if not products or (total > 0 and len(rows)>=total) or (total == 0 and len(products)<100):
                break
            page+=1
        aliases={value.lower() for value in service_aliases}
        matched=[row for row in rows if row.get('SubscriptionType') == 'PayAsYouGo' and
                 (str(row.get('ProductCode','')).lower() in aliases or str(row.get('ProductName','')).lower() in aliases)]
        if len(matched)!=1:
            raise ValueError('ambiguous_or_missing_aliyun_price_service')
        product=matched[0]['ProductCode']
        product_type=matched[0].get('ProductType','')
        description=self._query('DescribePricingModule',{'ProductCode':product,'ProductType':product_type,'SubscriptionType':'PayAsYouGo'},region=region)
        descriptions=((description.get('Data') or {}).get('ModuleList') or {}).get('Module') or []
        # FC is listed by BSS but its legacy pricing-module API returns no rates.
        if not descriptions and product == 'fc' and product_type == '':
            return self.fetch_fc_public_tariff(region=region)
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
            for card in self.fetch(product,modules,region=region,product_type=product_type):
                cards.append(replace(card,metadata={**card.metadata,'target':target,'module_configs':modules},snapshot_id='rate-'+digest({'card':card.snapshot_id,'modules':modules},length=24)))
        return cards

    def fetch_fc_public_tariff(self, *, region):
        url='https://help.aliyun.com/zh/functioncompute/pay-as-you-go-billing-methods'
        with self.opener(url, timeout=20) as response:
            html=response.read().decode('utf-8')
        return self.parse_fc_public_tariff(html, region=region, source_url=url)

    def parse_fc_public_tariff(self, html, *, region, source_url):
        parser=_TariffTables()
        parser.feed(html)
        tables=[table for table in parser.tables if table and len(table[0]) >= 3
                and table[0][1] == 'CU使用量（单位：CU）' and table[0][2] == '单价']
        if len(tables) != 1 or not region or region == 'default':
            raise ValueError('missing_or_ambiguous_fc_public_tariff')
        tiers=[];previous=Decimal('0')
        for row in tables[0][1:]:
            if len(row) < 3:
                raise ValueError('invalid_fc_public_tariff')
            price=re.fullmatch(r'([0-9]+(?:\.[0-9]+)?)元/CU',row[2])
            bounded=re.fullmatch(r'\(([^,]+),([^\]]+)\]',row[1])
            unbounded=re.fullmatch(r'>(.+)',row[1])
            if not price or not (bounded or unbounded):
                raise ValueError('invalid_fc_public_tariff')
            def quantity(value):
                if not re.fullmatch(r'[0-9]+(?:\.[0-9]+)?亿?',value):
                    raise ValueError('invalid_fc_public_tariff_range')
                return Decimal(value.rstrip('亿')) * (Decimal('100000000') if value.endswith('亿') else 1)
            lower=quantity(bounded[1] if bounded else unbounded[1])
            upper=quantity(bounded[2]) if bounded else None
            if lower != previous or (upper is not None and upper <= lower):
                raise ValueError('noncontiguous_fc_public_tariff')
            tiers.append(PriceTier(upper,Decimal(price[1])))
            previous=upper
        minimum=re.search(r'每小时消耗CU折算金额若低于([0-9]+(?:\.[0-9]+)?)元',
                          ''.join(''.join(parser.text).split()))
        if not tiers or tiers[-1].up_to is not None or minimum is None:
            raise ValueError('incomplete_fc_public_tariff')
        rule={'version':'aliyun_fc_public_tariff_v1','aggregation_scope':'resource_hour','aggregation_rounding_quantity':'1',
              'minimum_amount':minimum[1],'minimum_currency':'CNY','source_url':source_url}
        content={'service':'fc','sku':'CUUsage','region':region,'currency':'CNY',
                 'tiers':[(str(t.up_to) if t.up_to is not None else None,str(t.unit_price)) for t in tiers],
                 'billing_rule':rule,'source_url':source_url}
        now=utc_now()
        return [RateCard('rate-'+digest(content,length=24),'ali','fc','CUUsage',region,'pay_as_you_go',
                         'CU','CNY',tuple(tiers),now,now,'aliyun_fc_public_tariff',metadata={
                             'category':'compute_units','source_url':source_url,'billing_rule':rule,
                             'price_basis':'standard_public_price','tier_scope':'account_region_month',
                             'bss_pricing_modules':'empty'})]

    def fetch(self, product_code: str, modules: Iterable[dict[str, str]], *, region: str = "", product_type: str | None = None) -> list[RateCard]:
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
        if product_type is not None:
            request.add_query_param("ProductType", product_type)
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


class _TariffTables(HTMLParser):
    """Read the published table cells without depending on generated CSS names."""
    def __init__(self):
        super().__init__()
        self.tables=[];self.text=[];self.table=None;self.row=None;self.cell=None
        self.hidden=False

    def handle_starttag(self, tag, attrs):
        if tag in ('script','style'):self.hidden=True
        elif tag == 'table':self.table=[]
        elif tag == 'tr' and self.table is not None:self.row=[]
        elif tag in ('td','th') and self.row is not None:self.cell=[]

    def handle_data(self, data):
        if self.hidden:return
        self.text.append(data)
        if self.cell is not None:self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in ('script','style'):self.hidden=False
        elif tag in ('td','th') and self.cell is not None:
            self.row.append(''.join(''.join(self.cell).split()));self.cell=None
        elif tag == 'tr' and self.row is not None:
            self.table.append(self.row);self.row=None
        elif tag == 'table' and self.table is not None:
            self.tables.append(self.table);self.table=None
