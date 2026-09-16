from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from atsuite.market_data.cache import SnapshotStore
from atsuite.market_data.fx import ECBFxProvider, StaticFxProvider
from atsuite.market_data.providers import AlibabaPriceAdapter, AWSPriceAdapter, GCPCatalogAdapter, OpenExchangeRatesProvider


def _write(output: Path, payload) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True, default=str) + "\n", encoding="utf-8")


def _request(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("market request must be a JSON object")
    return payload


def main(argv=None):
    parser = argparse.ArgumentParser(description="Refresh or inspect ATSuite market snapshots")
    sub = parser.add_subparsers(dest="command", required=True)
    fx = sub.add_parser("fx")
    fx.add_argument("--source", choices=("ecb", "open_exchange_rates", "manual"), default="ecb")
    fx.add_argument("--output", type=Path, required=True)
    fx.add_argument("--base-currency", default="USD")
    fx.add_argument("--rates", type=Path)
    refresh = sub.add_parser("refresh")
    refresh.add_argument("--spec", type=Path, required=True)
    refresh.add_argument("--output", type=Path)
    refresh.add_argument("--rates", type=Path)
    price = sub.add_parser("price")
    price.add_argument("--provider", choices=("aws", "gcp", "ali"), required=True)
    price.add_argument("--request", type=Path, required=True, help="Provider-specific request JSON")
    price.add_argument("--output", type=Path, required=True)
    snapshot = sub.add_parser("store")
    snapshot.add_argument("--input", type=Path, required=True)
    snapshot.add_argument("--root", type=Path, required=True)
    args = parser.parse_args(argv)

    if args.command in {"fx", "refresh"}:
        if args.command == "refresh":
            spec = json.loads(args.spec.read_text(encoding="utf-8"))
            source = str((spec.get("market_data") or {}).get("fx_provider", "ecb"))
            base_currency = str((spec.get("objective") or {}).get("base_currency", "USD"))
            output = args.output or args.spec.with_name(f"{args.spec.stem}.market.json")
        else:
            source, base_currency, output = args.source, args.base_currency, args.output
        if source == "manual":
            if not args.rates:
                parser.error("manual FX requires --rates")
            raw = json.loads(args.rates.read_text(encoding="utf-8"))
            rates = raw.get("rates", raw) if isinstance(raw, dict) else raw
            provider = StaticFxProvider(rates)
        elif source == "open_exchange_rates":
            provider = OpenExchangeRatesProvider()
        else:
            provider = ECBFxProvider()
        fx_snapshot = provider.fetch(base_currency).to_dict()
        payload = fx_snapshot if args.command == "fx" else {"schema_version": 1, "fx": fx_snapshot, "pricing": {"coverage": "requires_service_sku_requests"}}
        _write(output, payload)
        print(output)
        return 0

    if args.command == "price":
        request = _request(args.request)
        if args.provider == "aws":
            cards = AWSPriceAdapter().fetch(str(request["service_code"]), request.get("filters", []), region=str(request.get("region", "")))
        elif args.provider == "gcp":
            cards = GCPCatalogAdapter().fetch(str(request["service_id"]), region=str(request.get("region", "")), currency=str(request.get("currency", "USD")))
        else:
            cards = AlibabaPriceAdapter().fetch(str(request["product_code"]), request.get("modules", []), region=str(request.get("region", "")))
        _write(args.output, {"schema_version": 1, "provider": args.provider, "coverage": "available" if cards else "missing", "rate_cards": [asdict(card) for card in cards]})
        print(args.output)
        return 0

    payload = _request(args.input)
    identifier = SnapshotStore(args.root).put(payload)
    print(identifier)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
