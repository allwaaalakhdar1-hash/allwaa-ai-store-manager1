#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = ROOT / "data" / "merchant_source_origin_probe.json"
OUT = ROOT / "data" / "merchant_feed_fetch_consistency_probe.json"
UA = "Mozilla/5.0 (compatible; AllwaaMerchantFeedFetchConsistency/1.0)"
DOUBLE_RE = re.compile(
    rb"attribute_pa_[^=&<]*=[^&<]*%25[0-9A-Fa-f]{2}",
    re.IGNORECASE,
)
ITEM_RE = re.compile(rb"<item[\s>]", re.IGNORECASE)
LINK_RE = re.compile(rb"<g:link>", re.IGNORECASE)


def add_cache_buster(url: str, token: str) -> str:
    p = urlsplit(url)
    pairs = parse_qsl(p.query, keep_blank_values=True)
    pairs.append(("allwaa_probe", token))
    return urlunsplit((p.scheme, p.netloc, p.path, urlencode(pairs), p.fragment))


def safe_url(url: str) -> str:
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, p.path, "", ""))


def response_record(label: str, url: str, headers: dict[str, str]) -> dict:
    s = requests.Session()
    req_headers = {
        "User-Agent": UA,
        "Accept": "application/xml,text/xml,*/*",
        **headers,
    }
    started = time.time()
    try:
        r = s.get(url, headers=req_headers, timeout=90, allow_redirects=True)
        body = r.content
        elapsed = round(time.time() - started, 3)
        return {
            "label": label,
            "ok": r.status_code == 200,
            "status": r.status_code,
            "finalUrlSafe": safe_url(r.url),
            "bytes": len(body),
            "sha256": hashlib.sha256(body).hexdigest().upper(),
            "items": len(ITEM_RE.findall(body)),
            "links": len(LINK_RE.findall(body)),
            "doubleEncoded": len(DOUBLE_RE.findall(body)),
            "elapsedSeconds": elapsed,
            "headers": {
                k: r.headers.get(k)
                for k in [
                    "Date", "Age", "ETag", "Last-Modified", "Cache-Control",
                    "CF-Cache-Status", "X-LiteSpeed-Cache", "X-LiteSpeed-Tag",
                    "X-Cache", "Via", "Server",
                ]
                if r.headers.get(k) is not None
            },
        }
    except Exception as exc:
        return {"label": label, "ok": False, "error": repr(exc)}


def main() -> int:
    print("=" * 110)
    print("ALLWAA MERCHANT FEED FETCH CONSISTENCY PROBE — READ ONLY")
    print("=" * 110)

    if not ORIGIN.exists():
        raise SystemExit(f"Missing {ORIGIN}. Run merchant_source_origin_probe.py first.")

    origin = json.loads(ORIGIN.read_text(encoding="utf-8"))
    sources = [
        s for s in origin.get("sources", [])
        if str(s.get("input") or "") == "FILE" and str(s.get("fetchUriSafe") or "")
    ]
    if not sources:
        raise SystemExit("No FILE sources with fetchUriSafe found.")

    token = str(int(time.time()))
    payload = {"readOnly": True, "sources": []}

    for source in sources:
        name = str(source.get("name") or "")
        display = str(source.get("displayName") or name)
        url = str(source.get("fetchUriSafe") or "")

        print("\n" + "-" * 110)
        print(f"SOURCE: {display} | {name}")
        print("URL   :", safe_url(url))

        attempts = [
            response_record("NORMAL", url, {}),
            response_record(
                "NO_CACHE_HEADERS",
                url,
                {"Cache-Control": "no-cache, no-store, max-age=0", "Pragma": "no-cache"},
            ),
            response_record(
                "CACHE_BUST_QUERY",
                add_cache_buster(url, token),
                {"Cache-Control": "no-cache, no-store, max-age=0", "Pragma": "no-cache"},
            ),
        ]

        for rec in attempts:
            if not rec.get("ok"):
                print(f"{rec['label']:18s} ERROR {rec.get('status', '-')} {rec.get('error', '')}")
                continue
            print(
                f"{rec['label']:18s} HTTP={rec['status']} items={rec['items']} links={rec['links']} "
                f"double={rec['doubleEncoded']} bytes={rec['bytes']} sha={rec['sha256'][:16]}"
            )
            h = rec.get("headers") or {}
            cache_bits = []
            for k in ["Age", "CF-Cache-Status", "X-LiteSpeed-Cache", "ETag", "Last-Modified"]:
                if h.get(k) is not None:
                    cache_bits.append(f"{k}={h[k]}")
            if cache_bits:
                print(" " * 20 + " | ".join(cache_bits))

        good = [x for x in attempts if x.get("ok")]
        unique_hashes = sorted({x.get("sha256") for x in good if x.get("sha256")})
        unique_double = sorted({int(x.get("doubleEncoded", 0)) for x in good})
        verdict = (
            "CONSISTENT"
            if len(unique_hashes) <= 1 and len(unique_double) <= 1
            else "CACHE_OR_EDGE_VARIANCE_DETECTED"
        )
        print("VERDICT:", verdict)

        payload["sources"].append({
            "name": name,
            "displayName": display,
            "urlSafe": safe_url(url),
            "attempts": attempts,
            "uniqueHashes": unique_hashes,
            "uniqueDoubleEncodedCounts": unique_double,
            "verdict": verdict,
        })

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 110)
    print("SUMMARY")
    print("=" * 110)
    for source in payload["sources"]:
        print(
            f"{source['displayName']} | {source['verdict']} | "
            f"double_counts={source['uniqueDoubleEncodedCounts']} | hashes={len(source['uniqueHashes'])}"
        )
    print("Saved:", OUT.relative_to(ROOT))
    print("✅ READ ONLY — NOTHING MODIFIED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
