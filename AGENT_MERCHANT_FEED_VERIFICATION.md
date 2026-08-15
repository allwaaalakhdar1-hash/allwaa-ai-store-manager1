# Agent task — Merchant feed repair verification

Status: VERIFY FIRST / READ ONLY

Do not make additional WordPress, Merchant, feed, robots, permalink, cache, database, category, or redirect changes until this verification is complete.

## Known pre-repair baseline

- FILE sources: 2
- XML items with link: 3737
- Double-encoded variation links in XML: 114
- Known current Merchant bad among those: 41
- Latent double-encoded links: 73
- Source 10647463497 / PRODUCTS SOURCE 8: 57 double-encoded
- Source 10647463716 / PRODUCTS SOURCE 9: 57 double-encoded
- Variant keys involved:
  - attribute_pa_flavor: 74
  - attribute_pa_المقاس: 24
  - attribute_pa_color: 10
  - attribute_pa_quantity: 6

Eight prior XML path differences were already proven correct by variation ID and MUST NOT be rewritten to the older proposed paths:
14901, 14902, 14938, 14939, 14950, 14951, 15258, 15259.

## Verification required after your current repair

1. Run the current repository audit:

```bash
cd /workspaces/allwaa-ai-store-manager1
git pull
PYTHONPATH=src .venv/bin/python tools/merchant_feed_double_encoding_scope.py
```

2. Required target:

```text
Double-encoded links in XML     : 0
Latent double-encoded in XML    : 0
```

If either is non-zero, STOP and report the remaining offer IDs, source, variant key, and exact link. Do not apply a broader rewrite.

3. Confirm variation semantics were preserved. For every changed variation link:
- keep `attribute_pa_*`
- remove only the extra percent-encoding layer
- do NOT replace Link with Link without parameters
- do NOT force parent-product URLs
- normal browser HTTP = 200
- Googlebot HTTP = 200
- selected variation/variation_id must still correspond to the offer ID where verifiable

4. Re-run the residual live diagnostic:

```bash
PYTHONPATH=src .venv/bin/python tools/merchant_final32_diagnose.py
```

Required live-site safety gates:

```text
LIVE_HTTP_ERROR     : 0
LIVE_REQUEST_ERROR  : 0
GOOGLEBOT_BLOCKED   : 0
ROBOTS_NOINDEX      : 0
CANONICAL_MISMATCH  : 0
```

5. Then run:

```bash
PYTHONPATH=src .venv/bin/python tools/merchant_unique_audit.py
```

Report only the new counts for:
- Approved / Pending / Disapproved (SHOPPING_ADS)
- landing_page_crawling_not_allowed
- landing_page_error
- landing_page_pending_crawl
- image_link_pending_crawl

Important: Merchant counts may lag; XML/live HTTP truth is the immediate acceptance criterion.

## Do not touch

- completed clean-link rollout (3459 verified / 249 excluded / 0 remaining)
- legacy redirect plugin (121/121 verified)
- robots
- .htaccess
- permalinks
- WoodMart/categories/database
- cache/OPcache/LiteSpeed broadly
- secondary data sources deletion
- the 8 variation-ID-confirmed current feed paths listed above

Return a concise `VERIFICATION SUMMARY` with before/after values and any remaining exceptions.
