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

## Current verified checkpoint — 2026-08-17

Manual independent fetches of the two current XML FILE sources established:

- PRODUCTS SOURCE 8: 1868 items / 1868 links / 49 double-encoded links
- PRODUCTS SOURCE 9: 1868 items / 1868 links / 49 double-encoded links
- Source 8 SHA-256: `A2B1602673FF9E4AF3A242A6F606A22F5E0840391EF176E8635DD16438143379`
- Source 9 SHA-256: `1B6417A98E30D7EAD901D94B23A13B6CEE91E192B5D49D85D437B836AC057FBD`
- The files are different.
- Exact bad-link intersection: 0
- Source-8-only exact bad links: 49
- Source-9-only exact bad links: 49
- Bad Product/Offer ID intersection: 49
- Source-8-only bad IDs: 0
- Source-9-only bad IDs: 0

Interpretation: this is one shared set of 49 affected product/offer IDs rendered differently by the two feeds. Do NOT treat it as 98 independent products and do NOT perform feed-by-feed blind rewrites.

## Required next diagnostic — cross-source pair classification

Run:

```bash
cd /workspaces/allwaa-ai-store-manager1
git pull
PYTHONPATH=src .venv/bin/python tools/merchant_feed_source_pair_compare.py
```

This tool is READ ONLY and must report:

```text
Same product IDs       : 49
Only left product IDs  : 0
Only right product IDs : 0
Same exact links       : 0
```

Also capture:

- Same semantic URLs
- Same paths
- Same variant-key sets
- CLASSIFICATIONS counts

Decision rule:

1. If most/all rows are `ENCODING_ONLY_DIFFERENCE`, fix only the extra encoding layer at the shared URL-generation point.
2. If rows are `SAME_PATH_KEYS_DIFFERENT_VALUES_OR_EXTRA_QUERY`, inspect per-source variation-value generation before any write.
3. If rows are `SAME_PATH_DIFFERENT_QUERY_SHAPE`, identify why source settings generate different parameter sets; do not normalize blindly.
4. If rows are `DIFFERENT_PATH`, validate variation/product semantics for each affected offer ID before any rewrite.
5. Never convert variation links to parent-product links merely to remove query parameters.

After this comparison, STOP and report the classification counts plus a small sample of offer IDs from each non-zero class. Do not perform another write wave until the shared root cause is identified.

## Full verification required after the eventual repair

1. Run the current repository audit:

```bash
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
