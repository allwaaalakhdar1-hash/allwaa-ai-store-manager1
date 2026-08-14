#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import re
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import requests

ROOT = Path(__file__).resolve().parents[1]
SITE = "https://allwaa-alakhdar.com"
INPUT = ROOT / "data" / "merchant_final32_diagnose.csv"
OUT_JSON = ROOT / "data" / "merchant_live404_route_map.json"
OUT_CSV = ROOT / "data" / "merchant_live404_route_map.csv"

UA_NORMAL = "Mozilla/5.0 (compatible; AllwaaMerchant404RouteMap/1.0)"
UA_GOOGLEBOT = (
    "Mozilla/5.0 (compatible; Googlebot/2.1; "
    "+http://www.google.com/bot.html)"
)


def strip_query(url: str) -> str:
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, p.path, "", ""))


def canonical_from_html(text: str) -> str:
    patterns = [
        r'<link[^>]+rel=["\'][^"\']*canonical[^"\']*["\'][^>]+href=["\']([^"\']+)',
        r'<link[^>]+href=["\']([^"\']+)["\'][^>]+rel=["\'][^"\']*canonical[^"\']*["\']',
    ]
    for pattern in patterns:
        m = re.search(pattern, text or "", flags=re.I)
        if m:
            return m.group(1).strip()
    return ""


def probe(session: requests.Session, url: str, user_agent: str) -> dict:
    try:
        r = session.get(
            url,
            timeout=30,
            allow_redirects=True,
            headers={"User-Agent": user_agent},
        )
        return {
            "status": r.status_code,
            "final": r.url,
            "chain": [f"{x.status_code} {x.url}" for x in r.history],
            "canonical": canonical_from_html(r.text) if "text/html" in r.headers.get("content-type", "") else "",
        }
    except Exception as exc:
        return {
            "status": None,
            "final": "",
            "chain": [],
            "canonical": "",
            "error": repr(exc),
        }


def get_store_product(session: requests.Session, product_id: int) -> dict:
    url = f"{SITE}/wp-json/wc/store/v1/products/{product_id}"
    try:
        r = session.get(url, timeout=30, headers={"User-Agent": UA_NORMAL})
        if r.status_code != 200:
            return {"ok": False, "status": r.status_code}
        data = r.json()
        slug = str(data.get("slug") or "").strip("/")
        permalink = str(data.get("permalink") or "").strip()
        if not permalink and slug:
            permalink = f"{SITE}/{slug}/"
        return {
            "ok": True,
            "status": 200,
            "id": data.get("id"),
            "name": data.get("name") or "",
            "slug": slug,
            "permalink": permalink,
        }
    except Exception as exc:
        return {"ok": False, "status": None, "error": repr(exc)}


def parse_product_id(offer_id: str) -> int | None:
    m = re.fullmatch(r"gla_(\d+)", (offer_id or "").strip())
    if m:
        return int(m.group(1))
    return None


def same_url(a: str, b: str) -> bool:
    return strip_query(a).rstrip("/") == strip_query(b).rstrip("/")


def pick(row: dict, *names: str) -> str:
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return str(value)
    return ""


def main() -> int:
    print("=" * 110)
    print("MERCHANT LIVE 404 ROUTE MAP — READ ONLY")
    print("=" * 110)

    if not INPUT.exists():
        raise SystemExit(f"Missing input: {INPUT}")

    with INPUT.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))

    live404 = [r for r in rows if (r.get("diagnosis") or "").strip() == "LIVE_HTTP_ERROR"]
    print("Input rows        :", len(rows))
    print("LIVE_HTTP_ERROR   :", len(live404))

    session = requests.Session()
    results: list[dict] = []

    for idx, row in enumerate(live404, 1):
        offer_id = pick(row, "offerId", "offer_id")
        issue = pick(row, "code", "issueCode", "issue_code")
        old_link = pick(row, "link", "url")
        title = pick(row, "title", "productTitle", "product_title")
        source = pick(row, "dataSource", "data_source", "source")
        product_id = parse_product_id(offer_id)

        rec = {
            "offerId": offer_id,
            "issue": issue,
            "oldLink": old_link,
            "oldBase": strip_query(old_link) if old_link else "",
            "title": title,
            "dataSource": source,
            "productId": product_id,
        }

        if product_id is None:
            rec["classification"] = "NO_NUMERIC_GLA_PRODUCT_ID"
            rec["safeRedirectCandidate"] = False
            results.append(rec)
            print(f"[{idx}/{len(live404)}] {offer_id} -> NO_NUMERIC_GLA_PRODUCT_ID")
            continue

        store = get_store_product(session, product_id)
        rec["storeApi"] = store

        current_url = store.get("permalink", "") if store.get("ok") else ""
        normal = probe(session, current_url, UA_NORMAL) if current_url else {}
        bot = probe(session, current_url, UA_GOOGLEBOT) if current_url else {}

        plain_product = probe(
            session,
            f"{SITE}/?post_type=product&p={product_id}",
            UA_NORMAL,
        )
        plain_post = probe(
            session,
            f"{SITE}/?p={product_id}",
            UA_NORMAL,
        )

        rec["currentUrl"] = current_url
        rec["currentNormal"] = normal
        rec["currentGooglebot"] = bot
        rec["plainProductQuery"] = plain_product
        rec["plainPostQuery"] = plain_post

        self_canonical = False
        if current_url and normal.get("status") == 200:
            canonical = normal.get("canonical") or ""
            self_canonical = (not canonical) or same_url(canonical, normal.get("final") or current_url)
        rec["selfCanonical"] = self_canonical

        if (
            store.get("ok")
            and current_url
            and normal.get("status") == 200
            and bot.get("status") == 200
            and self_canonical
            and old_link
            and not same_url(old_link, current_url)
        ):
            classification = "LEGACY_404_MAP_READY"
            safe = True
        elif store.get("ok") and current_url:
            classification = "CURRENT_PRODUCT_ROUTE_NEEDS_REVIEW"
            safe = False
        else:
            discovered = ""
            for candidate in (plain_product, plain_post):
                if candidate.get("status") == 200 and candidate.get("final"):
                    final = candidate["final"]
                    if final.rstrip("/") != SITE.rstrip("/"):
                        discovered = final
                        break
            if discovered:
                classification = "ROUTE_DISCOVERED_VIA_ID_QUERY"
                rec["discoveredUrl"] = discovered
                safe = bool(old_link and not same_url(old_link, discovered))
            else:
                classification = "PRODUCT_NOT_FOUND_OR_VARIATION"
                safe = False

        rec["classification"] = classification
        rec["safeRedirectCandidate"] = safe
        results.append(rec)

        target = rec.get("currentUrl") or rec.get("discoveredUrl") or "-"
        print(
            f"[{idx}/{len(live404)}] {offer_id} | {classification} | "
            f"old={rec['oldBase']} | target={target}"
        )

    by_class = Counter(r["classification"] for r in results)
    by_issue = Counter(r["issue"] for r in results)
    by_source = Counter((r.get("dataSource") or "<unknown>") for r in results)

    payload = {
        "readOnly": True,
        "input": str(INPUT.relative_to(ROOT)),
        "liveHttpErrorRows": len(results),
        "safeRedirectCandidates": sum(1 for r in results if r["safeRedirectCandidate"]),
        "byClassification": dict(sorted(by_class.items())),
        "byIssue": dict(sorted(by_issue.items())),
        "byDataSource": dict(sorted(by_source.items())),
        "results": results,
    }

    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    fields = [
        "offerId",
        "productId",
        "issue",
        "classification",
        "safeRedirectCandidate",
        "dataSource",
        "title",
        "oldLink",
        "oldBase",
        "currentUrl",
        "discoveredUrl",
        "storeApiStatus",
        "currentNormalStatus",
        "currentGooglebotStatus",
        "selfCanonical",
    ]
    with OUT_CSV.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for r in results:
            w.writerow(
                {
                    "offerId": r.get("offerId", ""),
                    "productId": r.get("productId", ""),
                    "issue": r.get("issue", ""),
                    "classification": r.get("classification", ""),
                    "safeRedirectCandidate": r.get("safeRedirectCandidate", False),
                    "dataSource": r.get("dataSource", ""),
                    "title": r.get("title", ""),
                    "oldLink": r.get("oldLink", ""),
                    "oldBase": r.get("oldBase", ""),
                    "currentUrl": r.get("currentUrl", ""),
                    "discoveredUrl": r.get("discoveredUrl", ""),
                    "storeApiStatus": (r.get("storeApi") or {}).get("status", ""),
                    "currentNormalStatus": (r.get("currentNormal") or {}).get("status", ""),
                    "currentGooglebotStatus": (r.get("currentGooglebot") or {}).get("status", ""),
                    "selfCanonical": r.get("selfCanonical", False),
                }
            )

    print()
    print("=" * 110)
    print("SUMMARY")
    print("=" * 110)
    print("LIVE_HTTP_ERROR rows     :", len(results))
    print("Safe redirect candidates :", payload["safeRedirectCandidates"])
    print("BY CLASSIFICATION")
    for key, value in sorted(by_class.items()):
        print(f"  {key:38s}: {value}")
    print("BY ISSUE")
    for key, value in sorted(by_issue.items()):
        print(f"  {key:38s}: {value}")
    print("BY DATA SOURCE")
    for key, value in sorted(by_source.items()):
        print(f"  {key}: {value}")
    print()
    print("Saved:", OUT_JSON.relative_to(ROOT))
    print("Saved:", OUT_CSV.relative_to(ROOT))
    print("✅ READ ONLY — NOTHING MODIFIED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
