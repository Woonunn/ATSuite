import math

from atsuite.recommender.model import MetricValue


class CurrencyNormalizer:
    def __init__(self, rate_table: dict[str, float] | None = None):
        self.rates = {"USD": 1.0, **(rate_table or {})}
        if self.rates["USD"] != 1.0 or any(
            not isinstance(currency, str) or not currency or currency != currency.upper()
            or isinstance(rate, bool) or not isinstance(rate, (int, float))
            or not math.isfinite(rate) or rate <= 0
            for currency, rate in self.rates.items()
        ):
            raise ValueError("FX rates must be positive finite USD per currency unit; USD must equal 1")

    def to_usd(self, amount: float | None, currency: str) -> MetricValue:
        if amount is None:
            return MetricValue(reason="Cost unavailable")
        if not math.isfinite(amount) or amount < 0:
            raise ValueError("Cost must be finite and nonnegative")
        rate = self.rates.get(currency)
        if rate is None:
            return MetricValue(reason=f"Missing {currency or 'unknown currency'} to USD exchange rate")
        return MetricValue(amount * rate, "available")


def min_max_normalize(values: dict[str, float | None]) -> dict[str, float]:
    present = {key: value for key, value in values.items() if value is not None}
    if not present:
        return {}
    if any(not math.isfinite(value) or value < 0 for value in present.values()):
        raise ValueError("Normalization requires finite nonnegative values")
    low, high = min(present.values()), max(present.values())
    return {key: 1.0 if high == low else (high - value) / (high - low) for key, value in present.items()}
