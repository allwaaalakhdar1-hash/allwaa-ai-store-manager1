#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
MANIFEST = DATA / "merchant_legacy404_redirect_manifest.csv"
JSON_OUT = DATA / "merchant_legacy404_postdeploy_verify.json"
CSV_OUT = DATA / "merchant_legacy404_postdeploy_verify.csv"
BASE = "https://allwaa-alakhdar.com"
TIMEOUT = 20
UA = "Mozilla/5.0 (compatible; AllwaaMerchantRedirectVerifier/1.0)"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def norm_path(value: str) -> str:
    p = urlparse(value)
    path = p.path if p.scheme else value
    if not path.startswith("/"):
        path = "/" + path
    if path != "/" and not path.endswith("/"):
        path += "/"
    return path


def main() -> int:
    print("=" * 110)
    print("ALLWAA MERCHANT LEGACY 404 — POST-DEPLOY VERIFY — READ ONLY")
    print("=" * 110)

    if not MANIFEST.exists():
        print(f"❌ Missing manifest: {MANIFEST}")
        return 2

    with MANIFEST.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))

    out: list[dict[str, str]] = []
    counts: Counter[str] = Counter()
    session = requests.Session()
    session.headers.update({"User-Agent": UA})

    for idx, row in enumerate(rows, start=1):
        old_path = norm_path(row["oldPath"])
        new_path = norm_path(row["newPath"])
        old_url = urljoin(BASE, old_path)
        expected_url = urljoin(BASE, new_path)

        rec = {
            "oldPath": old_path,
            "newPath": new_path,
            "oldStatus": "",
            "location": "",
            "locationPath": "",
            "finalStatus": "",
            "finalUrl": "",
            "classification": "",
            "error": "",
        }

        try:
            first = session.get(old_url, allow_redirects=False, timeout=TIMEOUT)
            rec["oldStatus"] = str(first.status_code)
            loc = first.headers.get("Location", "")
            rec["location"] = loc
            loc_abs = urljoin(old_url, loc) if loc else ""
            rec["locationPath"] = norm_path(loc_abs) if loc_abs else ""

            if first.status_code != 301:
                rec["classification"] = "FAIL_OLD_NOT_301"
            elif rec["locationPath"] != new_path:
                rec["classification"] = "FAIL_WRONG_LOCATION"
            else:
                final = session.get(expected_url, allow_redirects=True, timeout=TIMEOUT)
                rec["finalStatus"] = str(final.status_code)
                rec["finalUrl"] = final.url
                final_path = norm_path(final.url)
                if final.status_code == 200 and final_path == new_path:
                    rec["classification"] = "PASS"
                elif final.status_code != 200:
                    rec["classification"] = "FAIL_TARGET_NOT_200"
                else:
                    rec["classification"] = "FAIL_TARGET_PATH_CHANGED"
        except Exception as e:
            rec["classification"] = "ERROR"
            rec["error"] = repr(e)

        counts[rec["classification"]] += 1
        out.append(rec)
        mark = "✅" if rec["classification"] == "PASS" else "❌"
        print(
            f"[{idx:03d}/{len(rows):03d}] {mark} {old_path} -> "
            f"{rec['oldStatus']} {rec['locationPath']} -> {rec['finalStatus']}"
        )

    payload = {
        "timestamp": now_iso(),
        "manifest": str(MANIFEST.relative_to(ROOT)),
        "total": len(out),
        "byClassification": dict(sorted(counts.items())),
        "allPassed": counts.get("PASS", 0) == len(out),
        "rows": out,
    }
    JSON_OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    fieldnames = [
        "oldPath",
        "newPath",
        "oldStatus",
        "location",
        "locationPath",
        "finalStatus",
        "finalUrl",
        "classification",
        "error",
    ]
    with CSV_OUT.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(out)

    print("\n" + "=" * 110)
    print("SUMMARY")
    print("=" * 110)
    print("Manifest rows :", len(rows))
    for key, value in sorted(counts.items()):
        print(f"  {key:30s}: {value}")
    print("Saved:", JSON_OUT.relative_to(ROOT))
    print("Saved:", CSV_OUT.relative_to(ROOT))

    if counts.get("PASS", 0) == len(out):
        print("✅ ALL REDIRECTS VERIFIED")
        return 0

    print("❌ SOME REDIRECTS FAILED — REVIEW BEFORE KEEPING PLUGIN ACTIVE")
    return 4


if __name__ == "__main__":
    raise SystemExit(main())
