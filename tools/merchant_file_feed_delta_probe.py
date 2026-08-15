#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit, urlunsplit

import requests

ROOT = Path(__file__).resolve().parents[1]
INPUT = ROOT / "data" / "merchant_file_feed_link_probe.json"
OUT_JSON = ROOT / "data" / "merchant_file_feed_delta_probe.json"
OUT_CSV = ROOT / "data" / "merchant_file_feed_delta_probe.csv"

UA_NORMAL = "Mozilla/5.0 (compatible; AllwaaMerchantFeedDeltaProbe/1.0)"
UA_GOOGLEBOT = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
TRACKING_KEYS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "utm_id",
    "gclid", "gbraid", "wbraid", "fbclid", "msclkid",
}


def deep_unquote(value: str) -> str:
    cur = str(value or "")
    for _ in range(4):
        nxt = unquote(cur)
        if nxt == cur:
            break
        cur = nxt
    return cur


def canonical_path(url: str) -> str:
    p = urlsplit(url or "")
    path = p.path or "/"
    if path != "/" and not path.endswith("/"):
        path += "/"
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), path, "", ""))


def nontracking_pairs(url: str) -> list[tuple[str, str]]:
    pairs = []
    for k, v in parse_qsl(urlsplit(url or "").query, keep_blank_values=True):
        if k.lower() in TRACKING_KEYS:
            continue
        pairs.append((deep_unquote(k).lower(), deep_unquote(v).lower()))
    return sorted(pairs)


def tracking_pairs(url: str) -> list[tuple[str, str]]:
    pairs = []
    for k, v in parse_qsl(urlsplit(url or "").query, keep_blank_values=True):
        if k.lower() in TRACKING_KEYS:
            pairs.append((k.lower(), v))
    return sorted(pairs)


def probe(session: requests.Session, url: str, ua: str) -> dict:
    if not url:
        return {"status": None, "final": "", "error": "empty_url"}
    try:
        r = session.get(url, timeout=30, allow_redirects=True, headers={"User-Agent": ua})
        return {
            "status": r.status_code,
            "final": r.url,
            "redirects": len(r.history),
        }
    except Exception as exc:
        return {"status": None, "final": "", "error": repr(exc)}


def classify(feed: str, original: str, proposed: str) -> tuple[str, list[str]]:
    reasons: list[str] = []
    feed_path = canonical_path(feed)
    original_path = canonical_path(original)
    proposed_path = canonical_path(proposed)
    feed_non = nontracking_pairs(feed)
    orig_non = nontracking_pairs(original)
    prop_non = nontracking_pairs(proposed)

    if feed_path != proposed_path:
        reasons.append("feed_path_differs_from_proposed")
        return "XML_DIFFERENT_PATH", reasons

    if feed_non == prop_non:
        if "%25" in feed.lower():
            reasons.append("semantic_variant_matches_proposed_but_feed_remains_double_encoded")
            return "XML_SEMANTIC_MATCH_STILL_DOUBLE_ENCODED", reasons
        if tracking_pairs(feed) != tracking_pairs(proposed):
            reasons.append("only_tracking_parameters_differ")
            return "XML_EQUIVALENT_TRACKING_DIFF_ONLY", reasons
        reasons.append("semantic_nontracking_query_matches_proposed")
        return "XML_EQUIVALENT_TO_PROPOSED", reasons

    if feed_non == orig_non:
        if "%25" in feed.lower():
            reasons.append("feed_semantically_matches_original_and_is_double_encoded")
            return "XML_STILL_BAD_DOUBLE_ENCODED", reasons
        reasons.append("feed_semantically_matches_original")
        return "XML_MATCHES_ORIGINAL_SEMANTICS", reasons

    feed_attr = [(k, v) for k, v in feed_non if k.startswith("attribute_")]
    prop_attr = [(k, v) for k, v in prop_non if k.startswith("attribute_")]
    if feed_attr != prop_attr:
        reasons.append("variation_attribute_or_value_differs")
        reasons.append(f"feed_attrs={feed_attr}")
        reasons.append(f"proposed_attrs={prop_attr}")
        return "XML_DIFFERENT_VARIANT", reasons

    reasons.append("nontracking_query_differs_outside_variant_attributes")
    return "XML_OTHER_QUERY_DIFFERENCE", reasons


def main() -> int:
    print("=" * 110)
    print("ALLWAA MERCHANT FILE FEED DELTA PROBE — READ ONLY")
    print("=" * 110)

    if not INPUT.exists():
        raise SystemExit(f"Missing {INPUT}. Run merchant_file_feed_link_probe.py first.")

    data = json.loads(INPUT.read_text(encoding="utf-8"))
    rows = [r for r in data.get("results", []) if r.get("status") == "XML_LINK_DIFFERS_FROM_PLAN"]

    session = requests.Session()
    results = []
    counts: Counter[str] = Counter()

    print("Rows to inspect:", len(rows))

    for i, r in enumerate(rows, 1):
        feed = str(r.get("feedLink") or "")
        original = str(r.get("originalUrl") or "")
        proposed = str(r.get("proposedUrl") or "")
        cls, reasons = classify(feed, original, proposed)

        fn = probe(session, feed, UA_NORMAL)
        fb = probe(session, feed, UA_GOOGLEBOT)
        pn = probe(session, proposed, UA_NORMAL)
        pb = probe(session, proposed, UA_GOOGLEBOT)

        rec = {
            "offerId": r.get("offerId", ""),
            "dataSource": r.get("dataSource", ""),
            "sourceDisplayName": r.get("sourceDisplayName", ""),
            "classification": cls,
            "feedLink": feed,
            "originalUrl": original,
            "proposedUrl": proposed,
            "feedNonTracking": nontracking_pairs(feed),
            "proposedNonTracking": nontracking_pairs(proposed),
            "feedNormalStatus": fn.get("status"),
            "feedGooglebotStatus": fb.get("status"),
            "proposedNormalStatus": pn.get("status"),
            "proposedGooglebotStatus": pb.get("status"),
            "reasons": reasons,
        }
        results.append(rec)
        counts[cls] += 1
        print(
            f"[{i:02d}/{len(rows):02d}] {rec['offerId']} | {cls} | "
            f"feed={rec['feedNormalStatus']}/{rec['feedGooglebotStatus']} | "
            f"proposed={rec['proposedNormalStatus']}/{rec['proposedGooglebotStatus']}"
        )

    payload = {
        "readOnly": True,
        "input": str(INPUT.relative_to(ROOT)),
        "rows": len(results),
        "byClassification": dict(sorted(counts.items())),
        "results": results,
    }
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    fields = [
        "offerId", "dataSource", "sourceDisplayName", "classification",
        "feedNormalStatus", "feedGooglebotStatus", "proposedNormalStatus", "proposedGooglebotStatus",
        "feedLink", "originalUrl", "proposedUrl", "reasons",
    ]
    with OUT_CSV.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in results:
            row = dict(r)
            row["reasons"] = " | ".join(r.get("reasons", []))
            w.writerow({k: row.get(k, "") for k in fields})

    print("\n" + "=" * 110)
    print("SUMMARY")
    print("=" * 110)
    print("Rows inspected :", len(results))
    for k, v in sorted(counts.items()):
        print(f"  {k:48s}: {v}")
    print("Saved:", OUT_JSON.relative_to(ROOT))
    print("Saved:", OUT_CSV.relative_to(ROOT))
    print("✅ READ ONLY — NOTHING MODIFIED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
