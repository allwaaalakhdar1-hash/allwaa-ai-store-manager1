#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit

import requests

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = ROOT / "data" / "merchant_source_origin_probe.json"
OUT_JSON = ROOT / "data" / "merchant_feed_source_pair_compare.json"
OUT_CSV = ROOT / "data" / "merchant_feed_source_pair_compare.csv"
UA = "Mozilla/5.0 (compatible; AllwaaMerchantFeedSourcePairCompare/1.0)"


def localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def text_of(node: ET.Element, wanted: str) -> str:
    for child in list(node):
        if localname(child.tag).lower() == wanted.lower():
            return (child.text or "").strip()
    return ""


def decode_fully(value: str, rounds: int = 4) -> str:
    current = value
    for _ in range(rounds):
        nxt = unquote(current)
        if nxt == current:
            break
        current = nxt
    return current


def semantic_signature(url: str) -> dict:
    p = urlsplit(url)
    pairs = []
    for key, value in parse_qsl(p.query, keep_blank_values=True):
        pairs.append((decode_fully(key), decode_fully(value)))
    return {
        "path": decode_fully(p.path),
        "query": sorted(pairs),
    }


def variant_keys(url: str) -> list[str]:
    keys = []
    for key, _ in parse_qsl(urlsplit(url).query, keep_blank_values=True):
        decoded = decode_fully(key)
        if decoded.startswith("attribute_"):
            keys.append(decoded)
    return sorted(set(keys))


def fetch_bad_items(session: requests.Session, source: dict) -> dict[str, dict]:
    source_name = str(source.get("name") or "")
    display = str(source.get("displayName") or source_name)
    feed_url = str(source.get("fetchUriSafe") or "")

    response = session.get(feed_url, timeout=90)
    response.raise_for_status()
    root = ET.fromstring(response.content)

    rows: dict[str, dict] = {}
    item_count = 0
    link_count = 0
    for item in root.iter():
        if localname(item.tag).lower() not in {"item", "entry"}:
            continue
        item_count += 1
        offer_id = text_of(item, "id")
        link = text_of(item, "link")
        title = text_of(item, "title")
        if not offer_id or not link:
            continue
        link_count += 1
        if "%25" not in link.lower():
            continue
        rows[offer_id] = {
            "offerId": offer_id,
            "title": title,
            "link": link,
            "variantKeys": variant_keys(link),
            "semantic": semantic_signature(link),
        }

    return {
        "sourceName": source_name,
        "displayName": display,
        "feedUrl": feed_url,
        "items": item_count,
        "links": link_count,
        "bad": rows,
    }


def main() -> int:
    print("=" * 110)
    print("ALLWAA MERCHANT FILE SOURCE PAIR COMPARE — READ ONLY")
    print("=" * 110)

    if not ORIGIN.exists():
        raise SystemExit(f"Missing {ORIGIN}. Run merchant_source_origin_probe.py first.")

    origin = json.loads(ORIGIN.read_text(encoding="utf-8"))
    file_sources = [
        s for s in origin.get("sources", [])
        if str(s.get("input") or "") == "FILE" and str(s.get("fetchUriSafe") or "")
    ]
    if len(file_sources) != 2:
        raise SystemExit(f"Expected exactly 2 FILE sources, found {len(file_sources)}")

    session = requests.Session()
    session.headers.update({"User-Agent": UA, "Accept": "application/xml,text/xml,*/*"})

    left = fetch_bad_items(session, file_sources[0])
    right = fetch_bad_items(session, file_sources[1])

    left_ids = set(left["bad"])
    right_ids = set(right["bad"])
    common_ids = sorted(left_ids & right_ids)
    only_left = sorted(left_ids - right_ids)
    only_right = sorted(right_ids - left_ids)

    exact_same = 0
    semantic_same = 0
    same_path = 0
    same_variant_keys = 0
    classifications = Counter()
    comparison_rows = []

    for offer_id in common_ids:
        a = left["bad"][offer_id]
        b = right["bad"][offer_id]
        exact = a["link"] == b["link"]
        semantic = a["semantic"] == b["semantic"]
        path_same = a["semantic"]["path"] == b["semantic"]["path"]
        keys_same = a["variantKeys"] == b["variantKeys"]

        exact_same += int(exact)
        semantic_same += int(semantic)
        same_path += int(path_same)
        same_variant_keys += int(keys_same)

        if exact:
            kind = "EXACT_LINK_SAME"
        elif semantic:
            kind = "ENCODING_ONLY_DIFFERENCE"
        elif path_same and keys_same:
            kind = "SAME_PATH_KEYS_DIFFERENT_VALUES_OR_EXTRA_QUERY"
        elif path_same:
            kind = "SAME_PATH_DIFFERENT_QUERY_SHAPE"
        else:
            kind = "DIFFERENT_PATH"
        classifications[kind] += 1

        comparison_rows.append({
            "offerId": offer_id,
            "titleLeft": a["title"],
            "titleRight": b["title"],
            "classification": kind,
            "sameExactLink": exact,
            "sameSemanticUrl": semantic,
            "samePath": path_same,
            "sameVariantKeys": keys_same,
            "variantKeysLeft": a["variantKeys"],
            "variantKeysRight": b["variantKeys"],
            "linkLeft": a["link"],
            "linkRight": b["link"],
        })

    payload = {
        "readOnly": True,
        "sourceLeft": {
            "name": left["sourceName"],
            "displayName": left["displayName"],
            "items": left["items"],
            "links": left["links"],
            "doubleEncoded": len(left_ids),
        },
        "sourceRight": {
            "name": right["sourceName"],
            "displayName": right["displayName"],
            "items": right["items"],
            "links": right["links"],
            "doubleEncoded": len(right_ids),
        },
        "comparison": {
            "sameProductIds": len(common_ids),
            "onlyLeftProductIds": len(only_left),
            "onlyRightProductIds": len(only_right),
            "sameExactLinks": exact_same,
            "sameSemanticUrls": semantic_same,
            "samePaths": same_path,
            "sameVariantKeys": same_variant_keys,
            "classifications": dict(classifications),
        },
        "onlyLeftIds": only_left,
        "onlyRightIds": only_right,
        "rows": comparison_rows,
    }

    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    fields = [
        "offerId", "titleLeft", "titleRight", "classification",
        "sameExactLink", "sameSemanticUrl", "samePath", "sameVariantKeys",
        "variantKeysLeft", "variantKeysRight", "linkLeft", "linkRight",
    ]
    with OUT_CSV.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for row in comparison_rows:
            rendered = dict(row)
            rendered["variantKeysLeft"] = ",".join(row["variantKeysLeft"])
            rendered["variantKeysRight"] = ",".join(row["variantKeysRight"])
            writer.writerow({k: rendered.get(k, "") for k in fields})

    print(f"LEFT  {left['displayName']}: items={left['items']} links={left['links']} double={len(left_ids)}")
    print(f"RIGHT {right['displayName']}: items={right['items']} links={right['links']} double={len(right_ids)}")
    print("\nPRODUCT ID COMPARISON")
    print("Same product IDs       :", len(common_ids))
    print("Only left product IDs  :", len(only_left))
    print("Only right product IDs :", len(only_right))
    print("Same exact links       :", exact_same)
    print("Same semantic URLs     :", semantic_same)
    print("Same paths             :", same_path)
    print("Same variant-key sets  :", same_variant_keys)
    print("\nCLASSIFICATIONS")
    for key, count in classifications.most_common():
        print(f"  {key:48s}: {count}")
    print("\nSaved:", OUT_JSON.relative_to(ROOT))
    print("Saved:", OUT_CSV.relative_to(ROOT))
    print("✅ READ ONLY — NOTHING MODIFIED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
