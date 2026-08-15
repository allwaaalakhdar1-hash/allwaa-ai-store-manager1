#!/usr/bin/env python3
from __future__ import annotations

import html
import json
import re
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import requests

ROOT = Path(__file__).resolve().parents[1]
INPUT = ROOT / "data" / "merchant_file_feed_delta_probe.json"
OUT_JSON = ROOT / "data" / "merchant_file_feed_route_truth_probe.json"

SITE = "https://allwaa-alakhdar.com"
UA_NORMAL = "Mozilla/5.0 (compatible; AllwaaMerchantRouteTruth/1.0)"
UA_GOOGLEBOT = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"

CANONICAL_PATTERNS = [
    re.compile(r'<link[^>]+rel=["\'][^"\']*canonical[^"\']*["\'][^>]+href=["\']([^"\']+)', re.I),
    re.compile(r'<link[^>]+href=["\']([^"\']+)["\'][^>]+rel=["\'][^"\']*canonical[^"\']*["\']', re.I),
]
VARIATIONS_RE = re.compile(r"data-product_variations=(?:\"([^\"]+)\"|'([^']+)')", re.I | re.S)


def clean_path(url: str) -> str:
    if not url:
        return ""
    p = urlsplit(url)
    path = p.path or "/"
    if path != "/" and not path.endswith("/"):
        path += "/"
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), path, "", ""))


def extract_canonical(text: str) -> str:
    for pat in CANONICAL_PATTERNS:
        m = pat.search(text or "")
        if m:
            return m.group(1).strip()
    return ""


def extract_variation_ids(text: str) -> set[int]:
    m = VARIATIONS_RE.search(text or "")
    if not m:
        return set()
    raw = m.group(1) if m.group(1) is not None else m.group(2)
    try:
        data = json.loads(html.unescape(raw))
    except Exception:
        return set()
    out: set[int] = set()
    if isinstance(data, list):
        for row in data:
            try:
                out.add(int((row or {}).get("variation_id")))
            except Exception:
                pass
    return out


def extract_title(text: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", text or "", re.I | re.S)
    if not m:
        return ""
    return re.sub(r"\s+", " ", html.unescape(m.group(1))).strip()


def probe(session: requests.Session, url: str, ua: str) -> dict:
    if not url:
        return {"ok": False, "status": None, "finalUrl": "", "canonical": "", "variationIds": [], "title": "", "redirects": []}
    try:
        r = session.get(
            url,
            timeout=30,
            allow_redirects=True,
            headers={"User-Agent": ua, "Accept": "text/html,application/xhtml+xml"},
        )
        text = r.text if "text/html" in r.headers.get("content-type", "").lower() else ""
        return {
            "ok": True,
            "status": r.status_code,
            "finalUrl": r.url,
            "canonical": extract_canonical(text),
            "variationIds": sorted(extract_variation_ids(text)),
            "title": extract_title(text),
            "redirects": [
                {"status": h.status_code, "url": h.url, "location": h.headers.get("Location", "")}
                for h in r.history
            ],
        }
    except Exception as exc:
        return {"ok": False, "status": None, "finalUrl": "", "canonical": "", "variationIds": [], "title": "", "redirects": [], "error": repr(exc)}


def id_probe_urls(offer_id: str) -> list[tuple[str, str]]:
    if not str(offer_id).isdigit():
        return []
    oid = str(offer_id)
    return [
        ("product_variation_query", f"{SITE}/?post_type=product_variation&p={oid}"),
        ("product_query", f"{SITE}/?post_type=product&p={oid}"),
        ("plain_query", f"{SITE}/?p={oid}"),
    ]


def same_path(a: str, b: str) -> bool:
    return bool(a and b and clean_path(a) == clean_path(b))


def classify(offer_id: str, feed: dict, proposed: dict, id_probes: list[dict]) -> tuple[str, list[str]]:
    reasons: list[str] = []
    try:
        oid = int(offer_id)
    except Exception:
        oid = None

    feed_ids = set(feed.get("variationIds") or [])
    prop_ids = set(proposed.get("variationIds") or [])

    if oid is not None:
        in_feed = oid in feed_ids
        in_prop = oid in prop_ids
        if in_feed and not in_prop:
            reasons.append("offer_id_present_in_feed_page_variations_only")
            return "FEED_ROUTE_CONFIRMED_BY_VARIATION_ID", reasons
        if in_prop and not in_feed:
            reasons.append("offer_id_present_in_proposed_page_variations_only")
            return "PROPOSED_ROUTE_CONFIRMED_BY_VARIATION_ID", reasons
        if in_feed and in_prop:
            reasons.append("offer_id_present_in_both_pages_variations")

    for p in id_probes:
        final_url = p.get("finalUrl") or ""
        if p.get("status") == 200 and final_url:
            if same_path(final_url, feed.get("finalUrl") or ""):
                reasons.append(f"{p.get('kind')}_resolves_to_feed_path")
                return "FEED_ROUTE_CONFIRMED_BY_WP_ID_QUERY", reasons
            if same_path(final_url, proposed.get("finalUrl") or ""):
                reasons.append(f"{p.get('kind')}_resolves_to_proposed_path")
                return "PROPOSED_ROUTE_CONFIRMED_BY_WP_ID_QUERY", reasons

    feed_can = feed.get("canonical") or ""
    prop_can = proposed.get("canonical") or ""
    feed_final = feed.get("finalUrl") or ""
    prop_final = proposed.get("finalUrl") or ""

    if feed_can and same_path(feed_can, feed_final) and prop_can and same_path(prop_can, feed_final):
        reasons.append("both_pages_canonicalize_to_feed_path")
        return "FEED_ROUTE_CONFIRMED_BY_CANONICAL", reasons
    if prop_can and same_path(prop_can, prop_final) and feed_can and same_path(feed_can, prop_final):
        reasons.append("both_pages_canonicalize_to_proposed_path")
        return "PROPOSED_ROUTE_CONFIRMED_BY_CANONICAL", reasons

    if feed_can and same_path(feed_can, feed_final) and prop_can and same_path(prop_can, prop_final):
        reasons.append("both_paths_self_canonical_and_live")
        if oid is not None and oid in feed_ids and oid in prop_ids:
            return "BOTH_ROUTES_CONTAIN_VARIATION_ID", reasons
        return "AMBIGUOUS_TWO_LIVE_SELF_CANONICAL_ROUTES", reasons

    reasons.append("no_authoritative_route_signal")
    return "AMBIGUOUS_ROUTE_REVIEW", reasons


def main() -> int:
    print("=" * 110)
    print("ALLWAA MERCHANT FILE FEED ROUTE TRUTH PROBE — READ ONLY")
    print("=" * 110)

    if not INPUT.exists():
        raise SystemExit(f"Missing {INPUT}. Run merchant_file_feed_delta_probe.py first.")

    data = json.loads(INPUT.read_text(encoding="utf-8"))
    rows = [r for r in data.get("results", []) if r.get("classification") == "XML_DIFFERENT_PATH"]

    session = requests.Session()
    results = []
    counts: Counter[str] = Counter()

    print("Rows to inspect:", len(rows))

    for i, r in enumerate(rows, 1):
        oid = str(r.get("offerId") or "")
        feed_url = str(r.get("feedLink") or "")
        proposed_url = str(r.get("proposedUrl") or "")

        feed_n = probe(session, feed_url, UA_NORMAL)
        feed_b = probe(session, feed_url, UA_GOOGLEBOT)
        prop_n = probe(session, proposed_url, UA_NORMAL)
        prop_b = probe(session, proposed_url, UA_GOOGLEBOT)

        id_results = []
        for kind, url in id_probe_urls(oid):
            p = probe(session, url, UA_NORMAL)
            p["kind"] = kind
            p["requestedUrl"] = url
            id_results.append(p)

        cls, reasons = classify(oid, feed_n, prop_n, id_results)

        rec = {
            "offerId": oid,
            "dataSource": r.get("dataSource", ""),
            "classification": cls,
            "feedLink": feed_url,
            "proposedUrl": proposed_url,
            "feedNormalStatus": feed_n.get("status"),
            "feedGooglebotStatus": feed_b.get("status"),
            "proposedNormalStatus": prop_n.get("status"),
            "proposedGooglebotStatus": prop_b.get("status"),
            "feedFinalUrl": feed_n.get("finalUrl", ""),
            "proposedFinalUrl": prop_n.get("finalUrl", ""),
            "feedCanonical": feed_n.get("canonical", ""),
            "proposedCanonical": prop_n.get("canonical", ""),
            "feedVariationContainsOfferId": int(oid) in set(feed_n.get("variationIds") or []) if oid.isdigit() else False,
            "proposedVariationContainsOfferId": int(oid) in set(prop_n.get("variationIds") or []) if oid.isdigit() else False,
            "feedTitle": feed_n.get("title", ""),
            "proposedTitle": prop_n.get("title", ""),
            "idProbes": id_results,
            "reasons": reasons,
        }
        results.append(rec)
        counts[cls] += 1

        print(
            f"[{i:02d}/{len(rows):02d}] {oid} | {cls} | "
            f"feedVar={rec['feedVariationContainsOfferId']} proposedVar={rec['proposedVariationContainsOfferId']} | "
            f"feed={rec['feedNormalStatus']}/{rec['feedGooglebotStatus']} "
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

    print("\n" + "=" * 110)
    print("SUMMARY")
    print("=" * 110)
    print("Rows inspected :", len(results))
    for k, v in sorted(counts.items()):
        print(f"  {k:52s}: {v}")
    print("Saved:", OUT_JSON.relative_to(ROOT))
    print("✅ READ ONLY — NOTHING MODIFIED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
