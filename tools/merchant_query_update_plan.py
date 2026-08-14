#!/usr/bin/env python3
from __future__ import annotations

import base64
import json
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import requests

ROOT = Path(__file__).resolve().parents[1]
ACCOUNT_ID = "5580031112"
PREFLIGHT = ROOT / "data" / "merchant_query_variant_preflight.json"
DIAGNOSE = ROOT / "data" / "merchant_final32_diagnose.json"
OUT_JSON = ROOT / "data" / "merchant_query_update_plan.json"

READY_CLASSES = {
    "VARIANT_NORMALIZE_READY_EXACT",
    "VARIANT_NORMALIZE_READY_VALID",
    "CRON_STRIP_READY",
}

SCOPES = ["https://www.googleapis.com/auth/content"]


def norm_url(url: str) -> str:
    if not url:
        return ""
    p = urlsplit(url)
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path, p.query, ""))


def make_session() -> tuple[requests.Session | Any | None, str]:
    """Create an authenticated session without printing credentials."""
    try:
        import google.auth  # type: ignore
        from google.auth.transport.requests import AuthorizedSession  # type: ignore

        credentials, _ = google.auth.default(scopes=SCOPES)
        return AuthorizedSession(credentials), "google.auth.default"
    except Exception as adc_exc:
        # Codespaces sometimes has gcloud user auth but not ADC. Try access-token fallback.
        try:
            proc = subprocess.run(
                ["gcloud", "auth", "print-access-token"],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            token = proc.stdout.strip()
            if proc.returncode == 0 and token:
                s = requests.Session()
                s.headers.update({"Authorization": f"Bearer {token}"})
                return s, "gcloud-auth-token"
        except Exception:
            pass
        return None, f"AUTH_UNAVAILABLE: {adc_exc!r}"


def get_json(session: requests.Session | Any, url: str) -> dict[str, Any]:
    try:
        r = session.get(url, timeout=60)
        body: Any
        try:
            body = r.json()
        except Exception:
            body = {"raw": r.text[:1000]}
        return {"ok": r.status_code == 200, "status": r.status_code, "body": body}
    except Exception as exc:
        return {"ok": False, "status": None, "error": repr(exc), "body": {}}


def product_id_segment(language: str, feed_label: str, offer_id: str) -> str:
    raw = f"{language}~{feed_label}~{offer_id}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def product_link(product: dict[str, Any]) -> str:
    attrs = product.get("productAttributes") or product.get("attributes") or {}
    return str(attrs.get("link") or "")


def product_source(product: dict[str, Any]) -> str:
    return str(product.get("dataSource") or "")


def main() -> int:
    print("=" * 110)
    print("ALLWAA MERCHANT QUERY LINK UPDATE PLAN — READ ONLY")
    print("=" * 110)

    if not PREFLIGHT.exists() or not DIAGNOSE.exists():
        raise SystemExit("Missing preflight/diagnose JSON. Run the read-only diagnostics first.")

    pre = json.loads(PREFLIGHT.read_text(encoding="utf-8"))
    diag = json.loads(DIAGNOSE.read_text(encoding="utf-8"))

    candidates = [r for r in pre.get("results", []) if r.get("classification") in READY_CLASSES]
    diag_rows = diag.get("results", [])

    # Re-attach language/feedLabel using the exact original Merchant link as the strongest join key.
    meta_index: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    for r in diag_rows:
        key = (
            str(r.get("offerId") or ""),
            str(r.get("code") or ""),
            str(r.get("dataSource") or ""),
            str(r.get("link") or ""),
        )
        meta_index.setdefault(key, []).append(r)

    session, auth_method = make_session()
    if session is None:
        print("Auth:", auth_method)
        print("❌ READ ONLY PLAN COULD NOT QUERY MERCHANT API")
        return 2

    print("Auth method        :", auth_method)
    print("Ready candidates   :", len(candidates))

    sources = sorted({str(r.get("dataSource") or "") for r in candidates if r.get("dataSource")})
    source_info: dict[str, dict[str, Any]] = {}
    for source in sources:
        result = get_json(
            session,
            f"https://merchantapi.googleapis.com/datasources/v1/{source}",
        )
        body = result.get("body") or {}
        source_info[source] = {
            "ok": result.get("ok", False),
            "status": result.get("status"),
            "input": str(body.get("input") or ""),
            "displayName": str(body.get("displayName") or ""),
            "type": next(
                (
                    k
                    for k in (
                        "primaryProductDataSource",
                        "supplementalProductDataSource",
                        "localInventoryDataSource",
                        "regionalInventoryDataSource",
                    )
                    if k in body
                ),
                "",
            ),
        }

    results: list[dict[str, Any]] = []
    class_counts: Counter[str] = Counter()

    for i, c in enumerate(candidates, 1):
        key = (
            str(c.get("offerId") or ""),
            str(c.get("code") or ""),
            str(c.get("dataSource") or ""),
            str(c.get("originalUrl") or ""),
        )
        matches = meta_index.get(key, [])
        rec: dict[str, Any] = {
            "offerId": c.get("offerId", ""),
            "code": c.get("code", ""),
            "classification": c.get("classification", ""),
            "dataSource": c.get("dataSource", ""),
            "originalUrl": c.get("originalUrl", ""),
            "proposedUrl": c.get("cleanedUrl", ""),
            "language": "",
            "feedLabel": "",
            "sourceInput": "",
            "sourceDisplayName": "",
            "currentProcessedLink": "",
            "currentProcessedSource": "",
            "canApply": False,
        }

        if len(matches) != 1:
            rec["planStatus"] = "META_JOIN_AMBIGUOUS" if matches else "META_JOIN_MISSING"
            rec["metaMatches"] = len(matches)
            results.append(rec)
            class_counts[rec["planStatus"]] += 1
            print(f"[{i:02d}/{len(candidates):02d}] {rec['offerId']} | {rec['planStatus']}")
            continue

        meta = matches[0]
        language = str(meta.get("language") or "")
        feed_label = str(meta.get("feedLabel") or "")
        rec["language"] = language
        rec["feedLabel"] = feed_label

        s_info = source_info.get(str(rec["dataSource"]), {})
        rec["sourceInput"] = str(s_info.get("input") or "")
        rec["sourceDisplayName"] = str(s_info.get("displayName") or "")
        rec["sourceType"] = str(s_info.get("type") or "")
        rec["sourceStatus"] = s_info.get("status")

        if not s_info.get("ok"):
            rec["planStatus"] = "SOURCE_LOOKUP_FAILED"
            results.append(rec)
            class_counts[rec["planStatus"]] += 1
            print(f"[{i:02d}/{len(candidates):02d}] {rec['offerId']} | SOURCE_LOOKUP_FAILED")
            continue

        if rec["sourceInput"] != "API":
            rec["planStatus"] = "SOURCE_NOT_API"
            results.append(rec)
            class_counts[rec["planStatus"]] += 1
            print(
                f"[{i:02d}/{len(candidates):02d}] {rec['offerId']} | SOURCE_NOT_API "
                f"({rec['sourceInput']})"
            )
            continue

        if not language or not feed_label:
            rec["planStatus"] = "IDENTITY_INCOMPLETE"
            results.append(rec)
            class_counts[rec["planStatus"]] += 1
            print(f"[{i:02d}/{len(candidates):02d}] {rec['offerId']} | IDENTITY_INCOMPLETE")
            continue

        seg = product_id_segment(language, feed_label, str(rec["offerId"]))
        p_name = f"accounts/{ACCOUNT_ID}/products/{seg}"
        p_res = get_json(
            session,
            f"https://merchantapi.googleapis.com/products/v1/{p_name}",
        )
        rec["processedProductStatus"] = p_res.get("status")
        if not p_res.get("ok"):
            rec["planStatus"] = "PROCESSED_PRODUCT_LOOKUP_FAILED"
            results.append(rec)
            class_counts[rec["planStatus"]] += 1
            print(f"[{i:02d}/{len(candidates):02d}] {rec['offerId']} | PROCESSED_PRODUCT_LOOKUP_FAILED")
            continue

        product = p_res.get("body") or {}
        current_link = product_link(product)
        current_source = product_source(product)
        rec["currentProcessedLink"] = current_link
        rec["currentProcessedSource"] = current_source
        rec["processedProductName"] = str(product.get("name") or "")

        if current_source and current_source != rec["dataSource"]:
            rec["planStatus"] = "PROCESSED_SOURCE_MISMATCH"
        elif norm_url(current_link) != norm_url(str(rec["originalUrl"])):
            rec["planStatus"] = "LINK_ALREADY_CHANGED_OR_MISMATCH"
        elif not rec["proposedUrl"] or norm_url(str(rec["proposedUrl"])) == norm_url(current_link):
            rec["planStatus"] = "NO_EFFECT"
        else:
            rec["planStatus"] = "PATCH_READY"
            rec["canApply"] = True

        results.append(rec)
        class_counts[rec["planStatus"]] += 1
        print(
            f"[{i:02d}/{len(candidates):02d}] {rec['offerId']} | {rec['planStatus']} | "
            f"source={rec['sourceInput']}"
        )

    by_source = Counter(
        f"{r.get('dataSource','')} | {r.get('sourceInput','')} | {r.get('sourceDisplayName','')}"
        for r in results
    )
    ready = sum(1 for r in results if r.get("canApply"))

    payload = {
        "readOnly": True,
        "accountId": ACCOUNT_ID,
        "authMethod": auth_method,
        "input": str(PREFLIGHT.relative_to(ROOT)),
        "candidateCount": len(candidates),
        "patchReady": ready,
        "byPlanStatus": dict(sorted(class_counts.items())),
        "bySource": dict(sorted(by_source.items())),
        "sources": source_info,
        "results": results,
    }
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print()
    print("=" * 110)
    print("SUMMARY")
    print("=" * 110)
    print("Ready candidates :", len(candidates))
    print("PATCH_READY      :", ready)
    print("BY PLAN STATUS")
    for k, v in sorted(class_counts.items()):
        print(f"  {k:42s}: {v}")
    print("BY SOURCE")
    for k, v in sorted(by_source.items()):
        print(f"  {k}: {v}")
    print("Saved:", OUT_JSON.relative_to(ROOT))
    print("✅ READ ONLY — NOTHING MODIFIED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
