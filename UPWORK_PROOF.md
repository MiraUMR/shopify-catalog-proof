# Shopify ingestion — technical proof

I built a focused Python proof for the Shopify ingestion component of your ad-spy SaaS. It paginates a public storefront catalog, normalizes product/variant IDs and option mappings, preserves exact decimal prices and currency provenance, and persists idempotent product identities with per-run SQLite snapshots.

The included actual capture is from Shopify's public Dawn theme demo store: two complete endpoint traversals on November 1, 2026, each with 32 products and 266 variants. The second run retained 32 unique product identities and brought the snapshot count to 64. No price or availability changes occurred in those two live runs. Request logs, SHA-256 response evidence, CSV/JSON exports, a local report, and an offline verification script are included.

Failure handling is part of the proof: rate limiting, bounded retries, pagination anomaly detection, record quality logs, and explicit complete/partial/failed results. Incomplete collection cannot mark unseen products missing. Even a clean traversal uses `not_observed`, never a claim of confirmed delisting. Synthetic tests separately demonstrate injected price/availability changes and a page failure; these are clearly labeled and are not presented as market changes.

This is a public demo-store engineering sample, not a merchant production benchmark. The public catalog endpoint is not treated as an officially guaranteed full-store API. I do not infer stock quantity or sales from an availability flag. This sample covers Shopify only; Meta/TikTok collection and a complete SaaS are outside its demonstrated scope.

## Suggested 75-second recording

1. **0–20s:** Show the English report: public demo label, 32 products, 266 variants, two complete runs, zero live changes.
2. **20–35s:** Open the CSV/JSON exports. Point to stable IDs, mapped options, decimal price strings, CAD currency source and collection timestamps.
3. **35–50s:** Show `python verify_evidence.py` passing: 32 identities, 64 snapshots, 14 response hashes, 12 retained raw bodies.
4. **50–65s:** Show the passing synthetic test output and `evidence/synthetic/trace.json`. State explicitly that the changed price and timeout were injected fixtures.
5. **65–75s:** Point to partial-run safety and scope limitations in the README. Offer the ZIP/source for technical review.

Prepared as reviewable proposal material. No Upwork proposal has been submitted.
