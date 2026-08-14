#!/usr/bin/env python3
from __future__ import annotations

import csv
import html
import json
import re
from collections import Counter
from pathlib import Path
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

import requests

ROOT = Path(__file__).resolve().parents[1]
INPUT = ROOT / "data" / "merchant_final32_diagnose.json"
OUT_JSON = ROOT / "data" / "merchant_query_variant_preflight.json"
OUT_CSV = ROOT / "data" / "merchant_query_variant_preflight.csv"

UA_NORMAL = "Mozilla/5.0 (compatible; AllwaaMerchantVariantPreflight/1.0)"
UA_GOOGLEBOT = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"

TRACKING_KEYS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "utm_id",
    "gclid", "gbraid", "wbraid",
}

VARIATIONS_RE = re.compile(r"data-product_variations=(?:\"([^\"]+)\"|'([^']+)')", re.I | re.S)
CANONICAL_RE = re.compile(
    r'<link[^>]+rel=["\'][^"\']*canonical[^"\']*["\'][^>]+href=["\']([^"\']+)', re.I
)
CANONICAL_RE_REV = re.compile(
    r'<link[^>]+href=["\']([^"\']+)["\'][^>]+rel=["\'][^"\']*canonical[^"\']*["\']', re.I
)


def decode_one_more(s: str) -> str:
    try:
        return unquote(s)
    except Exception:
        return s


def normalize_token(s: str) -> str:
    prev = str(s or "")
    for _ in range(3):
        cur = unquote(prev)
        if cur == prev:
            break
        prev = cur
    return prev.strip().lower()


def build_candidates(url: str) -> tuple[str, str, list[tuple[str, str]], bool, list[str]]:
    p = urlsplit(url)
    pairs = parse_qsl(p.query, keep_blank_values=True)
    non_tracking = [(k, v) for k, v in pairs if k.lower() not in TRACKING_KEYS]
    keys = [k for k, _ in non_tracking]
    has_cron = any(k == "doing_wp_cron" for k in keys)
    has_double = "%25" in url.lower()

    if has_cron:
        cleaned_pairs: list[tuple[str, str]] = []
    else:
        cleaned_pairs = [(decode_one_more(k), decode_one_more(v)) for k, v in non_tracking]

    cleaned_query = urlencode(cleaned_pairs, doseq=True, quote_via=quote)
    cleaned = urlunsplit((p.scheme, p.netloc, p.path, cleaned_query, ""))
    base = urlunsplit((p.scheme, p.netloc, p.path, "", ""))
    return cleaned, base, cleaned_pairs, has_double, keys


def extract_canonical(text: str) -> str:
    m = CANONICAL_RE.search(text or "") or CANONICAL_RE_REV.search(text or "")
    return m.group(1).strip() if m else ""


def extract_variations(text: str) -> list[dict]:
    m = VARIATIONS_RE.search(text or "")
    if not m:
        return []
    raw = m.group(1) if m.group(1) is not None else m.group(2)
    try:
        decoded = html.unescape(raw)
        data = json.loads(decoded)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def probe(session: requests.Session, url: str, ua: str) -> dict:
    try:
        r = session.get(
            url,
            timeout=30,
            allow_redirects=True,
            headers={"User-Agent": ua, "Accept": "text/html,application/xhtml+xml"},
        )
        text = r.text if "text/html" in r.headers.get("content-type", "") else ""
        return {
            "ok": True,
            "status": r.status_code,
            "finalUrl": r.url,
            "canonical": extract_canonical(text),
            "variations": extract_variations(text),
            "html": text[:1_500_000],
            "redirects": [
                {"status": h.status_code, "url": h.url, "location": h.headers.get("Location", "")}
                for h in r.history
            ],
        }
    except Exception as exc:
        return {"ok": False, "status": None, "error": repr(exc), "variations": [], "html": ""}


def variation_matches(variations: list[dict], query_pairs: list[tuple[str, str]]) -> list[int]:
    if not variations or not query_pairs:
        return []
    wanted = {normalize_token(k): normalize_token(v) for k, v in query_pairs if k.startswith("attribute_")}
    if not wanted:
        return []

    matches: list[int] = []
    for v in variations:
        attrs = v.get("attributes") or {}
        attrs_norm = {normalize_token(k): normalize_token(val) for k, val in attrs.items()}
        ok = True
        for k, val in wanted.items():
            if k not in attrs_norm or attrs_norm[k] != val:
                ok = False
                break
        if ok:
            try:
                matches.append(int(v.get("variation_id")))
            except Exception:
                pass
    return matches


def expected_id(offer_id: str) -> int | None:
    s = str(offer_id or "").strip()
    if s.isdigit():
        return int(s)
    m = re.fullmatch(r"gla_(\d+)", s)
    return int(m.group(1)) if m else None


def main() -> int:
    print("=" * 110)
    print("ALLWAA MERCHANT QUERY / VARIANT PREFLIGHT — READ ONLY")
    print("=" * 110)

    if not INPUT.exists():
        raise SystemExit(f"Missing input: {INPUT}")

    data = json.loads(INPUT.read_text(encoding="utf-8"))
    rows = [r for r in data.get("results", []) if r.get("diagnosis") == "QUERY_PARAMETER_REVIEW"]
    print("Input QUERY_PARAMETER_REVIEW :", len(rows))

    session = requests.Session()
    results: list[dict] = []
    counts: Counter[str] = Counter()

    for idx, r in enumerate(rows, 1):
        link = str(r.get("link") or "")
        offer_id = str(r.get("offerId") or "")
        cleaned, base, cleaned_pairs, has_double, raw_keys = build_candidates(link)
        has_cron = any(k == "doing_wp_cron" for k in raw_keys)
        has_variant = any(k.startswith("attribute_") for k, _ in cleaned_pairs)

        clean_normal = probe(session, cleaned, UA_NORMAL)
        clean_bot = probe(session, cleaned, UA_GOOGLEBOT)
        base_normal = probe(session, base, UA_NORMAL)
        base_bot = probe(session, base, UA_GOOGLEBOT)

        variations = clean_normal.get("variations") or []
        matched_ids = variation_matches(variations, cleaned_pairs)
        exp_id = expected_id(offer_id)

        clean_ok = clean_normal.get("status") == 200 and clean_bot.get("status") == 200
        base_ok = base_normal.get("status") == 200 and base_bot.get("status") == 200

        if has_cron:
            if clean_ok and cleaned == base:
                classification = "CRON_STRIP_READY"
                proposed_action = "REPLACE_WITH_BASE_URL"
            else:
                classification = "CRON_REVIEW"
                proposed_action = "NONE"
        elif has_variant and has_double:
            if clean_ok and exp_id is not None and exp_id in matched_ids:
                classification = "VARIANT_NORMALIZE_READY_EXACT"
                proposed_action = "REPLACE_WITH_SINGLE_ENCODED_VARIANT_URL"
            elif clean_ok and matched_ids:
                classification = "VARIANT_NORMALIZE_READY_VALID"
                proposed_action = "REPLACE_WITH_SINGLE_ENCODED_VARIANT_URL"
            else:
                classification = "VARIANT_NORMALIZE_REVIEW"
                proposed_action = "NONE"
        elif has_variant:
            classification = "VARIANT_KEEP_AS_IS"
            proposed_action = "NONE"
        else:
            classification = "OTHER_QUERY_REVIEW"
            proposed_action = "NONE"

        rec = {
            "offerId": offer_id,
            "code": r.get("code", ""),
            "dataSource": r.get("dataSource", ""),
            "originalUrl": link,
            "cleanedUrl": cleaned,
            "baseUrl": base,
            "classification": classification,
            "proposedAction": proposed_action,
            "doubleEncoded": has_double,
            "queryPairs": cleaned_pairs,
            "expectedId": exp_id,
            "matchedVariationIds": matched_ids,
            "variationCountInHtml": len(variations),
            "cleanNormalStatus": clean_normal.get("status"),
            "cleanGooglebotStatus": clean_bot.get("status"),
            "baseNormalStatus": base_normal.get("status"),
            "baseGooglebotStatus": base_bot.get("status"),
            "cleanCanonical": clean_normal.get("canonical", ""),
            "cleanFinalUrl": clean_normal.get("finalUrl", ""),
            "cleanGooglebotFinalUrl": clean_bot.get("finalUrl", ""),
        }
        results.append(rec)
        counts[classification] += 1
        print(
            f"[{idx:02d}/{len(rows):02d}] {offer_id} | {classification} | "
            f"clean={rec['cleanNormalStatus']}/{rec['cleanGooglebotStatus']} | matches={matched_ids[:5]}"
        )

    payload = {
        "readOnly": True,
        "input": str(INPUT.relative_to(ROOT)),
        "total": len(results),
        "byClassification": dict(sorted(counts.items())),
        "readyToChange": sum(1 for r in results if r["proposedAction"] != "NONE"),
        "results": results,
    }
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    fields = [
        "offerId", "code", "classification", "proposedAction", "dataSource",
        "doubleEncoded", "expectedId", "matchedVariationIds", "variationCountInHtml",
        "cleanNormalStatus", "cleanGooglebotStatus", "baseNormalStatus", "baseGooglebotStatus",
        "originalUrl", "cleanedUrl", "baseUrl", "cleanCanonical",
    ]
    with OUT_CSV.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in results:
            row = dict(r)
            row["matchedVariationIds"] = ",".join(str(x) for x in r["matchedVariationIds"])
            w.writerow({k: row.get(k, "") for k in fields})

    print()
    print("=" * 110)
    print("SUMMARY")
    print("=" * 110)
    print("Total query cases :", len(results))
    print("Ready to change   :", payload["readyToChange"])
    print("BY CLASSIFICATION")
    for k, v in sorted(counts.items()):
        print(f"  {k:42s}: {v}")
    print("Saved:", OUT_JSON.relative_to(ROOT))
    print("Saved:", OUT_CSV.relative_to(ROOT))
    print("✅ READ ONLY — NOTHING MODIFIED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
