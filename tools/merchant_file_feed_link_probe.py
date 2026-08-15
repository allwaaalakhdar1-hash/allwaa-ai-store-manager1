#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlsplit

import requests

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "data" / "merchant_query_update_plan.json"
ORIGIN = ROOT / "data" / "merchant_source_origin_probe.json"
OUT_JSON = ROOT / "data" / "merchant_file_feed_link_probe.json"
OUT_CSV = ROOT / "data" / "merchant_file_feed_link_probe.csv"

UA = "Mozilla/5.0 (compatible; AllwaaMerchantFileFeedProbe/1.0)"


def localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def text_of(node: ET.Element, wanted: str) -> str:
    for child in list(node):
        if localname(child.tag).lower() == wanted.lower():
            return (child.text or "").strip()
    return ""


def safe_url(url: str) -> str:
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc}{p.path}"


def norm(url: str) -> str:
    return (url or "").strip()


def main() -> int:
    print("=" * 110)
    print("ALLWAA MERCHANT FILE FEED LINK PROBE — READ ONLY")
    print("=" * 110)

    if not PLAN.exists() or not ORIGIN.exists():
        raise SystemExit("Missing update-plan/source-origin JSON. Run prior READ ONLY probes first.")

    plan = json.loads(PLAN.read_text(encoding="utf-8"))
    origin = json.loads(ORIGIN.read_text(encoding="utf-8"))

    candidates = [
        r for r in plan.get("results", [])
        if r.get("planStatus") == "SOURCE_NOT_API" and r.get("sourceInput") == "FILE"
    ]

    sources = {}
    for s in origin.get("sources", []):
        name = str(s.get("name") or "")
        uri = str(s.get("fetchUriSafe") or "")
        if name and uri:
            sources[name] = uri

    needed_by_source = defaultdict(list)
    for r in candidates:
        needed_by_source[str(r.get("dataSource") or "")].append(r)

    session = requests.Session()
    session.headers.update({"User-Agent": UA, "Accept": "application/xml,text/xml,*/*"})

    results = []
    counts = Counter()

    print(f"FILE candidates : {len(candidates)}")
    print(f"FILE sources    : {len(needed_by_source)}")

    for source, rows in sorted(needed_by_source.items()):
        feed_url = sources.get(source, "")
        print("\n" + "-" * 110)
        print(f"SOURCE: {source} | rows={len(rows)} | feed={safe_url(feed_url) if feed_url else '-'}")
        if not feed_url:
            for r in rows:
                rec = {
                    "offerId": r.get("offerId", ""),
                    "dataSource": source,
                    "status": "FEED_URL_MISSING",
                    "feedLink": "",
                    "originalUrl": r.get("originalUrl", ""),
                    "proposedUrl": r.get("proposedUrl", ""),
                }
                results.append(rec)
                counts[rec["status"]] += 1
            continue

        try:
            resp = session.get(feed_url, timeout=90)
            print(f"HTTP {resp.status_code} | bytes={len(resp.content)}")
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
        except Exception as exc:
            print("FETCH/PARSE ERROR:", repr(exc))
            for r in rows:
                rec = {
                    "offerId": r.get("offerId", ""),
                    "dataSource": source,
                    "status": "FEED_FETCH_OR_PARSE_ERROR",
                    "error": repr(exc),
                    "feedLink": "",
                    "originalUrl": r.get("originalUrl", ""),
                    "proposedUrl": r.get("proposedUrl", ""),
                }
                results.append(rec)
                counts[rec["status"]] += 1
            continue

        items = [x for x in root.iter() if localname(x.tag).lower() in {"item", "entry"}]
        print(f"Feed items: {len(items)}")

        by_id = defaultdict(list)
        for item in items:
            oid = text_of(item, "id")
            link = text_of(item, "link")
            if oid:
                by_id[oid].append({"link": link})

        for r in rows:
            oid = str(r.get("offerId") or "")
            original = str(r.get("originalUrl") or "")
            proposed = str(r.get("proposedUrl") or "")
            matches = by_id.get(oid, [])
            feed_links = sorted({norm(x.get("link", "")) for x in matches if x.get("link")})

            if not matches:
                status = "OFFER_NOT_FOUND_IN_XML"
            elif len(feed_links) != 1:
                status = "MULTIPLE_LINKS_IN_XML"
            else:
                feed_link = feed_links[0]
                if norm(feed_link) == norm(original):
                    if "%25" in feed_link.lower():
                        status = "XML_CONFIRMS_DOUBLE_ENCODED_SOURCE"
                    else:
                        status = "XML_MATCHES_MERCHANT_ORIGINAL"
                elif norm(feed_link) == norm(proposed):
                    status = "XML_ALREADY_CLEAN"
                else:
                    status = "XML_LINK_DIFFERS_FROM_PLAN"

            rec = {
                "offerId": oid,
                "dataSource": source,
                "sourceDisplayName": r.get("sourceDisplayName", ""),
                "classification": r.get("classification", ""),
                "status": status,
                "matchCount": len(matches),
                "feedLink": feed_links[0] if len(feed_links) == 1 else " || ".join(feed_links[:5]),
                "originalUrl": original,
                "proposedUrl": proposed,
            }
            results.append(rec)
            counts[status] += 1
            print(f"  {oid} | {status}")

    payload = {
        "readOnly": True,
        "candidateCount": len(candidates),
        "byStatus": dict(sorted(counts.items())),
        "results": results,
    }
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    fields = [
        "offerId", "dataSource", "sourceDisplayName", "classification", "status",
        "matchCount", "feedLink", "originalUrl", "proposedUrl",
    ]
    with OUT_CSV.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in results:
            w.writerow({k: r.get(k, "") for k in fields})

    print("\n" + "=" * 110)
    print("SUMMARY")
    print("=" * 110)
    print("FILE candidates :", len(candidates))
    for k, v in sorted(counts.items()):
        print(f"  {k:42s}: {v}")
    print("Saved:", OUT_JSON.relative_to(ROOT))
    print("Saved:", OUT_CSV.relative_to(ROOT))
    print("✅ READ ONLY — NOTHING MODIFIED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
