#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "data" / "merchant_query_update_plan.json"
OUT = ROOT / "data" / "merchant_source_origin_probe.json"
ACCOUNT_ID = "5580031112"


def safe_uri(value: str) -> str:
    if not value:
        return ""
    try:
        p = urlsplit(value)
        host = p.hostname or ""
        port = f":{p.port}" if p.port else ""
        return urlunsplit((p.scheme, host + port, p.path, "", ""))
    except Exception:
        return "<redacted-or-unparseable>"


def get_json(session, url: str) -> dict:
    try:
        r = session.get(url, timeout=60)
        try:
            body = r.json()
        except Exception:
            body = {"raw": r.text[:500]}
        return {"ok": r.status_code == 200, "status": r.status_code, "body": body}
    except Exception as exc:
        return {"ok": False, "status": None, "error": repr(exc), "body": {}}


def main() -> int:
    print("=" * 110)
    print("ALLWAA MERCHANT SOURCE ORIGIN PROBE — READ ONLY")
    print("=" * 110)

    if not PLAN.exists():
        raise SystemExit(f"Missing {PLAN}. Run merchant_query_update_plan_local_auth.py first.")

    tools_dir = str(ROOT / "tools")
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    from merchant_query_update_plan_local_auth import make_session

    session, auth_method = make_session()
    print("Auth method:", auth_method)

    plan = json.loads(PLAN.read_text(encoding="utf-8"))
    rows = list(plan.get("results", []))
    source_counts = Counter(str(r.get("dataSource") or "") for r in rows)

    source_names = sorted(x for x in source_counts if x)
    source_results = []

    print("\nDATA SOURCES")
    for source in source_names:
        res = get_json(session, f"https://merchantapi.googleapis.com/datasources/v1/{source}")
        body = res.get("body") or {}
        file_input = body.get("fileInput") or {}
        fetch_settings = file_input.get("fetchSettings") or {}

        rec = {
            "name": source,
            "candidateRows": source_counts[source],
            "status": res.get("status"),
            "displayName": str(body.get("displayName") or ""),
            "input": str(body.get("input") or ""),
            "fileInputType": str(file_input.get("fileInputType") or ""),
            "fileName": str(file_input.get("fileName") or ""),
            "fetchUriSafe": safe_uri(str(fetch_settings.get("fetchUri") or "")),
            "fetchEnabled": fetch_settings.get("enabled"),
            "fetchFrequency": str(fetch_settings.get("frequency") or ""),
            "contentLanguage": str((body.get("primaryProductDataSource") or {}).get("contentLanguage") or ""),
            "feedLabel": str((body.get("primaryProductDataSource") or {}).get("feedLabel") or ""),
        }

        if rec["input"] == "FILE":
            latest = get_json(
                session,
                f"https://merchantapi.googleapis.com/datasources/v1/{source}/fileUploads/latest",
            )
            lb = latest.get("body") or {}
            rec["latestUpload"] = {
                "status": latest.get("status"),
                "processingState": str(lb.get("processingState") or ""),
                "itemsTotal": lb.get("itemsTotal"),
                "itemsCreated": lb.get("itemsCreated"),
                "itemsUpdated": lb.get("itemsUpdated"),
                "uploadTime": str(lb.get("uploadTime") or ""),
                "issueCount": len(lb.get("issues") or []),
            }

        source_results.append(rec)
        print(
            f"  {source} | rows={rec['candidateRows']} | input={rec['input']} | "
            f"name={rec['displayName']} | fileType={rec['fileInputType'] or '-'} | "
            f"file={rec['fileName'] or '-'} | fetch={rec['fetchUriSafe'] or '-'}"
        )

    failed = [r for r in rows if r.get("planStatus") == "PROCESSED_PRODUCT_LOOKUP_FAILED"]
    failed_offer_ids = {str(r.get("offerId") or "") for r in failed}
    resolved = []

    if failed_offer_ids:
        print("\nFAILED API LOOKUPS — ACCOUNT-WIDE SEARCH")
        token = None
        page = 0
        while True:
            page += 1
            params = {"pageSize": 1000}
            if token:
                params["pageToken"] = token
            try:
                rr = session.get(
                    f"https://merchantapi.googleapis.com/products/v1/accounts/{ACCOUNT_ID}/products",
                    params=params,
                    timeout=60,
                )
                if rr.status_code != 200:
                    print("  products.list HTTP", rr.status_code)
                    break
                payload = rr.json()
            except Exception as exc:
                print("  products.list error:", repr(exc))
                break

            for product in payload.get("products", []):
                oid = str(product.get("offerId") or "")
                if oid in failed_offer_ids:
                    resolved.append({
                        "offerId": oid,
                        "name": str(product.get("name") or ""),
                        "base64EncodedName": str(product.get("base64EncodedName") or ""),
                        "contentLanguage": str(product.get("contentLanguage") or ""),
                        "feedLabel": str(product.get("feedLabel") or ""),
                        "dataSource": str(product.get("dataSource") or ""),
                        "link": str((product.get("productAttributes") or {}).get("link") or ""),
                    })
            token = payload.get("nextPageToken")
            if not token or page >= 20:
                break

        by_offer = Counter(x["offerId"] for x in resolved)
        for oid in sorted(failed_offer_ids):
            matches = [x for x in resolved if x["offerId"] == oid]
            print(f"  {oid} | matches={by_offer.get(oid, 0)}")
            for x in matches[:5]:
                print(
                    f"    lang={x['contentLanguage']} feed={x['feedLabel']} "
                    f"source={x['dataSource']} link={x['link']}"
                )

    payload = {
        "readOnly": True,
        "authMethod": auth_method,
        "sources": source_results,
        "failedLookupOfferIds": sorted(failed_offer_ids),
        "failedLookupMatches": resolved,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 110)
    print("SUMMARY")
    print("=" * 110)
    for rec in source_results:
        print(
            f"{rec['displayName'] or rec['name']} | input={rec['input']} | "
            f"rows={rec['candidateRows']} | fileType={rec['fileInputType'] or '-'}"
        )
    if failed_offer_ids:
        print("Failed API offer IDs :", len(failed_offer_ids))
        print("Resolved matches     :", len(resolved))
    print("Saved:", OUT.relative_to(ROOT))
    print("✅ READ ONLY — NOTHING MODIFIED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
