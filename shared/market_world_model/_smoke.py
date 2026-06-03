"""Smoke test for the public market_world_model API."""
from shared.market_world_model import api as market

print("=== forecast Dubai Marina (price_per_sqft, 6 mo) ===")
fc = market.forecast("Dubai Marina", "price_per_sqft", 6)
print(f"ok={fc['ok']} current={fc.get('current')} predicted={fc.get('predicted')} "
      f"pct={fc.get('pct_change')} source={fc.get('source')}")
print(f"points={len(fc.get('points', []))}")

print("\n=== ask 'Что будет с Marina через 6 месяцев?' ===")
print(market.ask("Что будет с Marina через 6 месяцев?", lang="ru"))

print("\n=== compare ===")
print(market.compare("Dubai Marina", "Business Bay", "price_per_sqft", 6))

print("\n=== what_if 'supply +30%' ===")
wf = market.what_if("Business Bay", "supply +30%", "price_per_sqft", 6)
print(f"ok={wf['ok']} base={wf.get('base_predicted')} scenario={wf.get('scenario_predicted')} shift={wf.get('scenario_shift_pct')}")

print("\n=== ask compare ===")
print(market.ask("Dubai Marina vs Business Bay через 12 месяцев", lang="ru"))
