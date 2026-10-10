import io
import json
import unittest
from decimal import Decimal
from unittest.mock import Mock, patch

from atsuite.market_data.providers.aliyun import AlibabaPriceAdapter


class AlibabaPriceCatalogTests(unittest.TestCase):
    candidate = {'region': 'us-east-1', 'resources_by_target': {'tool': {'cpu': 1, 'memory_mb': 1024}}}
    fc = {'ProductCode': 'fc', 'ProductName': '函数计算', 'ProductType': '', 'SubscriptionType': 'PayAsYouGo'}
    package = {**fc, 'ProductType': 'fc_cu_dp_cn', 'SubscriptionType': 'Subscription'}

    def fetch_pages(self, pages):
        requests = []

        def query(request):
            action, params = request.get_action_name(), request.get_query_params()
            requests.append((action, dict(params)))
            if action == 'QueryProductList':
                data = pages[int(params['PageNum']) - 1]
            elif action == 'DescribePricingModule':
                data = {'ModuleList': {'Module': [{'ModuleCode': 'cpu', 'PriceType': 'Hour',
                                                   'ConfigList': {'ConfigList': ['Region', 'CPU']}}]}}
            elif action == 'GetPayAsYouGoPrice':
                data = {'Currency': 'CNY', 'ModuleDetails': {'ModuleDetail': [{'ModuleCode': 'cpu', 'UnitPrice': '0.25'}]}}
            else:
                self.fail('unexpected API action: ' + action)
            return json.dumps({'Success': True, 'Data': data}).encode()

        client = Mock()
        client.do_action_with_exception.side_effect = query
        adapter = AlibabaPriceAdapter(client)
        cards = adapter.fetch_candidate(self.candidate, service_aliases=('fc', '函数计算'))
        return cards, requests

    def test_fc_on_later_page_excludes_resource_package_and_preserves_product_type(self):
        cards, requests = self.fetch_pages([
            {'TotalCount': 2, 'ProductList': {'Product': [self.package]}},
            {'TotalCount': 2, 'ProductList': {'Product': [self.fc]}},
        ])
        catalogs = [p for action, p in requests if action == 'QueryProductList']
        self.assertEqual([p['PageNum'] for p in catalogs], [1, 2])
        self.assertTrue(all(p['QueryTotalCount'] == 'true' for p in catalogs))
        self.assertTrue(all('SubscriptionType' not in p for p in catalogs))
        for action, params in requests:
            if action != 'QueryProductList':
                self.assertIn('ProductType', params)
                self.assertEqual(params['ProductType'], '')
        price = requests[-1][1]
        self.assertEqual(price['ModuleList.1.Config'], 'Region:us-east-1,CPU:1')
        self.assertEqual(cards[0].metadata['target'], 'tool')
        self.assertEqual(cards[0].tiers[0].unit_price, Decimal('0.25'))

    def test_zero_or_absent_total_does_not_truncate_full_page(self):
        unrelated = {'ProductCode': 'other', 'SubscriptionType': 'PayAsYouGo'}
        for total in [None, 0]:
            with self.subTest(total=total):
                page = {'ProductList': {'Product': [unrelated] * 100}}
                if total is not None:
                    page['TotalCount'] = total
                cards, requests = self.fetch_pages([page, {'ProductList': {'Product': [self.fc]}}])
                self.assertEqual(len(cards), 1)
                self.assertEqual(sum(action == 'QueryProductList' for action, _ in requests), 2)

    def test_empty_or_ambiguous_catalog_is_not_a_price(self):
        for products in [[], [self.package], [self.fc, {**self.fc, 'ProductType': 'another'}]]:
            with self.subTest(products=products):
                with self.assertRaisesRegex(ValueError, 'ambiguous_or_missing_aliyun_price_service'):
                    self.fetch_pages([{'ProductList': {'Product': products}}])


class AlibabaPublicTariffTests(unittest.TestCase):
    url = 'https://help.aliyun.com/zh/functioncompute/pay-as-you-go-billing-methods'
    html = '''<table><tr><th>阶梯</th><th>CU 使用量（单位：CU）</th><th>单价</th>
        <th>官网折扣单价</th></tr>
        <tr><td>1</td><td>(0,2亿]</td><td><span>0.00011</span> 元/CU</td><td>0.000088元/CU</td></tr>
        <tr><td>2</td><td>(2亿,10亿]</td><td>0.00010元/CU</td><td>0.000080元/CU</td></tr>
        <tr><td>3</td><td>&gt;10亿</td><td>0.00009元/CU</td><td>0.000072元/CU</td></tr></table>
        <p>每小时消耗CU折算金额若低于<span>0.01</span>元，将按0.01元计费。</p>'''

    def parse(self, html=None, region='us-east-1'):
        return AlibabaPriceAdapter().parse_fc_public_tariff(
            self.html if html is None else html, region=region, source_url=self.url)[0]

    def test_standard_prices_tiers_and_identity_follow_live_content(self):
        with patch('atsuite.market_data.providers.aliyun.utc_now', return_value='2026-10-10T01:00:00Z'):
            card = self.parse()
        with patch('atsuite.market_data.providers.aliyun.utc_now', return_value='2026-10-10T02:00:00Z'):
            later = self.parse()
        self.assertEqual(card.currency, 'CNY')
        self.assertEqual(card.unit, 'CU')
        self.assertEqual([t.up_to for t in card.tiers], [Decimal('200000000'), Decimal('1000000000'), None])
        self.assertEqual([t.unit_price for t in card.tiers], list(map(Decimal, ['0.00011', '0.00010', '0.00009'])))
        self.assertEqual(card.metadata['source_url'], self.url)
        self.assertEqual(card.metadata['billing_rule']['minimum_currency'], 'CNY')
        self.assertEqual(card.snapshot_id, later.snapshot_id)
        self.assertNotEqual(card.fetched_at, later.fetched_at)
        self.assertNotEqual(card.snapshot_id, self.parse(self.html.replace('0.00011', '0.00012')).snapshot_id)
        self.assertNotEqual(card.snapshot_id, self.parse(region='cn-hangzhou').snapshot_id)

    def test_only_empty_fc_modules_use_the_live_public_source(self):
        adapter = AlibabaPriceAdapter(opener=Mock(side_effect=lambda *a, **k: io.BytesIO(self.html.encode())))
        adapter._query = Mock(side_effect=[
            {'Data': {'ProductList': {'Product': [AlibabaPriceCatalogTests.fc]}}},
            {'Data': {'ModuleList': {'Module': []}}},
        ])
        cards = adapter.fetch_candidate(AlibabaPriceCatalogTests.candidate, service_aliases=('fc',))
        self.assertEqual(cards[0].source, 'aliyun_fc_public_tariff')
        adapter.opener.assert_called_once_with(self.url, timeout=20)
        adapter.opener.reset_mock()
        adapter._query = Mock(side_effect=ValueError('aliyun_catalog_query_failed:Forbidden'))
        with self.assertRaisesRegex(ValueError, 'Forbidden'):
            adapter.fetch_candidate(AlibabaPriceCatalogTests.candidate, service_aliases=('fc',))
        adapter.opener.assert_not_called()

    def test_broken_or_ambiguous_public_rates_are_not_prices(self):
        for html in [
            '', self.html + self.html,
            self.html.replace('(2亿,10亿]', '(3亿,10亿]'),
            self.html.replace('&gt;10亿', '(10亿,20亿]'),
            self.html.replace('0.00011</span>', 'unknown</span>'),
            self.html.split('<p>')[0],
            self.html.split('<p>')[0] + '<script>每小时消耗CU折算金额若低于0.01元</script>',
        ]:
            with self.subTest(html=html), self.assertRaises(ValueError):
                self.parse(html)

    def test_freeze_and_reprice_use_the_cny_hourly_minimum(self):
        from atsuite.market_data.fx import StaticFxProvider
        from atsuite.market_data.service import OnlineMarketData, freeze_market_data, price_candidate_ledger

        adapter = Mock()
        adapter.fetch_candidate.side_effect = lambda *a, **k: [self.parse()]
        candidates = [{'candidate_id': p, 'provider': p, 'region': 'us-east-1'}
                      for p in ('ali_fc', 'ali_agentrun')]
        frozen = freeze_market_data(candidates, {'pricing': 'online'}, service=OnlineMarketData(
            ali=adapter, fx_provider=StaticFxProvider({'CNY': '7'})))
        self.assertEqual(frozen['status'], 'frozen')
        self.assertEqual(len(frozen['rate_cards']), 1)
        self.assertEqual(frozen['billing_rules']['compute_units']['minimum_currency'], 'CNY')
        self.assertEqual(frozen['billing_rules']['compute_units']['source_url'], self.url)
        card = frozen['rate_cards'][0]
        usage = {'candidate_id': 'ali_fc', 'run_id': 'r', 'category': 'compute_units', 'quantity': '.1',
                 'unit': card['unit'], 'provider': card['provider'], 'service': card['service'],
                 'region': card['region'], 'sku': card['sku'], 'billing_mode': card['billing_mode'],
                 'currency': card['currency'], 'account_id': 'acct', 'resource_id': 'f',
                 'usage_start': card['observed_at'], 'billing_period': card['observed_at'][:7]}
        result = price_candidate_ledger([usage], frozen, target_currency='CNY')
        self.assertEqual(result['coverage'], 'complete')
        self.assertEqual(Decimal(result['amount']), Decimal('.01'))
        usd = price_candidate_ledger([usage], frozen)
        self.assertEqual(usd['coverage'], 'complete')
        self.assertEqual(Decimal(usd['amount']), Decimal('.01') / Decimal('7'))


class EcbHttpsTests(unittest.TestCase):
    def test_default_opener_loads_application_ca_and_keeps_certificate_verification(self):
        import ssl
        from atsuite.market_data.fx import ECBFxProvider

        body = b'<Envelope><Cube time="2026-10-09"><Cube currency="USD" rate="1.1"/></Cube></Envelope>'
        with patch('atsuite.market_data.fx.urllib.request.urlopen', return_value=io.BytesIO(body)) as opener:
            provider = ECBFxProvider()
            provider.fetch('USD')
        context = opener.call_args.kwargs['context']
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertIn('x509_ca', context.cert_store_stats())


if __name__ == '__main__':
    unittest.main()
