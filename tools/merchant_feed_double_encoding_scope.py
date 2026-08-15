#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

import requests

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = ROOT / "data" / "merchant_source_origin_probe.json"
KNOWN = ROOT / "data" / "merchant_file_feed_link_probe.json"
OUT_JSON = ROOT / "data" / "merchant_feed_double_encoding_scope.json"
OUT_CSV = ROOT / "data" / "merchant_feed_double_encoding_scope.csv"
UA = "Mozilla/5.0 (compatible; AllwaaMerchantFeedDoubleEncodingScope/1.0)"
TRACKING_KEYS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "utm_id",
    "gclid", "gbraid", "wbraid", "fbclid", "msclkid",
}


def localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def text_of(node: ET.Element, wanted: str) -> str:
    for child in list(node):
        if localname(child.tag).lower() == wanted.lower():
            return (child.text or "").strip()
    return ""


def decode_one_more(value: str) -> str:
    try:
        return unquote(value)
    except Exception:
        return value


def single_encode_variant_url(url: str) -> str:
    p = urlsplit(url)
    pairs = parse_qsl(p.query, keep_blank_values=True)
    cleaned = []
    for k, v in pairs:
        if k.lower() in TRACKING_KEYS:
            cleaned.append((k, v))
        else:
            cleaned.append((decode_one_more(k), decode_one_more(v)))
    q = urlencode(cleaned, doseq=True, quote_via=quote)
    return urlunsplit((p.scheme, p.netloc, p.path, q, ""))


def nontracking_keys(url: str) -> list[str]:
    keys = []
    for k, _ in parse_qsl(urlsplit(url).query, keep_blank_values=True):
        if k.lower() not in TRACKING_KEYS:
            keys.append(decode_one_more(k))
    return keys


def safe_url(url: str) -> str:
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, p.path, "", ""))


def main() -> int:
    print("=" * 110)
    print("ALLWAA MERCHANT FEED DOUBLE-ENCODING SCOPE — READ ONLY")
    print("=" * 110)

    if not ORIGIN.exists():
        raise SystemExit(f"Missing {ORIGIN}. Run merchant_source_origin_probe.py first.")

    origin = json.loads(ORIGIN.read_text(encoding="utf-8"))
    known_rows = []
    if KNOWN.exists():
        known_rows = json.loads(KNOWN.read_text(encoding="utf-8")).get("results", [])

    known_bad = {
        (str(r.get("dataSource") or ""), str(r.get("offerId") or ""))
        for r in known_rows
        if r.get("status") == "XML_CONFIRMS_DOUBLE_ENCODED_SOURCE"
    }

    file_sources = [
        s for s in origin.get("sources", [])
        if str(s.get("input") or "") == "FILE" and str(s.get("fetchUriSafe") or "")
    ]

    session = requests.Session()
    session.headers.update({"User-Agent": UA, "Accept": "application/xml,text/xml,*/*"})

    results = []
    total_counts = Counter()
    source_counts = defaultdict(Counter)
    key_counts = Counter()

    for source in file_sources:
        source_name = str(source.get("name") or "")
        display = str(source.get("displayName") or source_name)
        feed_url = str(source.get("fetchUriSafe") or "")
        print("\n" + "-" * 110)
        print(f"SOURCE: {display} | {safe_url(feed_url)}")

        try:
            resp = session.get(feed_url, timeout=90)
            print(f"HTTP {resp.status_code} | bytes={len(resp.content)}")
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
        except Exception as exc:
            print("FETCH/PARSE ERROR:", repr(exc))
            continue

        items = [x for x in root.iter() if localname(x.tag).lower() in {"item", "entry"}]
        print("Feed items:", len(items))

        for item in items:
            oid = text_of(item, "id")
            link = text_of(item, "link")
            if not oid or not link:
                continue

            total_counts["items_with_link"] += 1
            source_counts[source_name]["items_with_link"] += 1

            has_double = "%25" in link.lower()
            if not has_double:
                continue

            total_counts["double_encoded_links"] += 1
            source_counts[source_name]["double_encoded_links"] += 1

            keys = nontracking_keys(link)
            variant_keys = [k for k in keys if k.startswith("attribute_")]
            for k in variant_keys:
                key_counts[k] += 1

            cleaned = single_encode_variant_url(link)
            known_current = (source_name, oid) in known_bad

            if known_current:
                status = "KNOWN_CURRENT_MERCHANT_BAD"
            else:
                status = "LATENT_DOUBLE_ENCODED_IN_FEED"

            rec = {
                "offerId": oid,
                "dataSource": source_name,
                "sourceDisplayName": display,
                "status": status,
                "variantKeys": variant_keys,
                "feedLink": link,
                "singleEncodedCandidate": cleaned,
            }
            results.append(rec)
            total_counts[status] += 1
            source_counts[source_name][status] += 1

        sc = source_counts[source_name]
        print("Items with link       :", sc["items_with_link"])
        print("Double-encoded links  :", sc["double_encoded_links"])
        print("Known Merchant bad    :", sc["KNOWN_CURRENT_MERCHANT_BAD"])
        print("Latent in feed        :", sc["LATENT_DOUBLE_ENCODED_IN_FEED"])

    payload = {
        "readOnly": True,
        "sourceCount": len(file_sources),
        "summary": dict(total_counts),
        "bySource": {k: dict(v) for k, v in source_counts.items()},
        "byVariantKey": dict(key_counts.most_common()),
        "results": results,
    }
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    fields = [
        "offerId", "dataSource", "sourceDisplayName", "status", "variantKeys",
        "feedLink", "singleEncodedCandidate",
    ]
    with OUT_CSV.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in results:
            row = dict(r)
            row["variantKeys"] = ",".join(r.get("variantKeys", []))
            w.writerow({k: row.get(k, "") for k in fields})

    print("\n" + "=" * 110)
    print("SUMMARY")
    print("=" * 110)
    print("FILE sources                    :", len(file_sources))
    print("Items with link                 :", total_counts["items_with_link"])
    print("Double-encoded links in XML     :", total_counts["double_encoded_links"])
    print("Known current Merchant bad      :", total_counts["KNOWN_CURRENT_MERCHANT_BAD"])
    print("Latent double-encoded in XML    :", total_counts["LATENT_DOUBLE_ENCODED_IN_FEED"])
    print("BY SOURCE")
    for source_name, c in sorted(source_counts.items()):
        print(
            f"  {source_name} | links={c['items_with_link']} | double={c['double_encoded_links']} | "
            f"known={c['KNOWN_CURRENT_MERCHANT_BAD']} | latent={c['LATENT_DOUBLE_ENCODED_IN_FEED']}"
        )
    print("BY VARIANT KEY")
    for k, v in key_counts.most_common():
        print(f"  {k:50s}: {v}")
    print("Saved:", OUT_JSON.relative_to(ROOT))
    print("Saved:", OUT_CSV.relative_to(ROOT))
    print("✅ READ ONLY — NOTHING MODIFIED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
