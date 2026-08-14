#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
INPUT = DATA / "merchant_live404_route_map.csv"
PHP_OUT = DATA / "allwaa-merchant-legacy404-redirects.php"
JSON_OUT = DATA / "merchant_legacy404_redirect_manifest.json"
CSV_OUT = DATA / "merchant_legacy404_redirect_manifest.csv"

ALLOWED_HOSTS = {"allwaa-alakhdar.com", "www.allwaa-alakhdar.com"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def truthy(v: str | None) -> bool:
    return str(v or "").strip().lower() in {"1", "true", "yes", "y"}


def first(row: dict[str, str], *keys: str) -> str:
    for k in keys:
        v = row.get(k)
        if v is not None and str(v).strip():
            return str(v).strip()
    return ""


def normalized_path(url: str) -> str:
    p = urlparse(url)
    if p.scheme not in {"http", "https"}:
        raise ValueError(f"unsupported scheme: {url}")
    host = (p.hostname or "").lower()
    if host not in ALLOWED_HOSTS:
        raise ValueError(f"unexpected host {host}: {url}")
    path = p.path or "/"
    if not path.startswith("/"):
        path = "/" + path
    # WordPress product permalinks here conventionally end with slash.
    if path != "/" and not path.endswith("/"):
        path += "/"
    return path


def php_quote(s: str) -> str:
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def main() -> int:
    print("=" * 110)
    print("ALLWAA MERCHANT LEGACY 404 — EXACT REDIRECT PACK BUILDER")
    print("=" * 110)
    print("INPUT:", INPUT.relative_to(ROOT))

    if not INPUT.exists():
        print(f"❌ Missing input: {INPUT}")
        return 2

    with INPUT.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))

    eligible: list[dict[str, str]] = []
    rejected: list[dict[str, str]] = []
    errors: list[str] = []

    for i, row in enumerate(rows, start=2):
        cls = first(row, "classification")
        safe = truthy(first(row, "safeRedirectCandidate"))
        if cls != "LEGACY_404_MAP_READY" or not safe:
            continue

        offer_id = first(row, "offerId", "offer_id")
        product_id = first(row, "productId", "product_id")
        old_url = first(row, "oldBase", "oldUrl", "old_url", "link")
        new_url = first(row, "currentUrl", "discoveredUrl", "newUrl", "new_url")
        issue = first(row, "code", "issueCode", "issue")
        source = first(row, "dataSource", "source")

        if not old_url or not new_url:
            rejected.append({"line": str(i), "offerId": offer_id, "reason": "missing old/new URL"})
            continue

        try:
            old_path = normalized_path(old_url)
            new_path = normalized_path(new_url)
        except Exception as e:
            rejected.append({"line": str(i), "offerId": offer_id, "reason": repr(e)})
            continue

        if old_path == new_path:
            rejected.append({"line": str(i), "offerId": offer_id, "reason": "old path equals new path"})
            continue

        eligible.append(
            {
                "offerId": offer_id,
                "productId": product_id,
                "issue": issue,
                "dataSource": source,
                "oldUrl": old_url,
                "newUrl": new_url,
                "oldPath": old_path,
                "newPath": new_path,
            }
        )

    by_old: dict[str, set[str]] = defaultdict(set)
    for r in eligible:
        by_old[r["oldPath"]].add(r["newPath"])

    conflicts = {old: sorted(targets) for old, targets in by_old.items() if len(targets) > 1}
    if conflicts:
        print("❌ CONFLICTS DETECTED — refusing to build redirect pack")
        for old, targets in sorted(conflicts.items()):
            print(" ", old, "=>", targets)
        return 3

    # Deduplicate exact old->new mappings. Multiple Merchant offers/destinations may point to same legacy path.
    unique_map: dict[str, str] = {}
    evidence: dict[str, list[dict[str, str]]] = defaultdict(list)
    for r in eligible:
        unique_map[r["oldPath"]] = r["newPath"]
        evidence[r["oldPath"]].append(r)

    manifest_rows = []
    for old_path, new_path in sorted(unique_map.items()):
        ev = evidence[old_path]
        manifest_rows.append(
            {
                "oldPath": old_path,
                "newPath": new_path,
                "offerIds": ",".join(sorted({x["offerId"] for x in ev if x["offerId"]})),
                "productIds": ",".join(sorted({x["productId"] for x in ev if x["productId"]})),
                "issues": ",".join(sorted({x["issue"] for x in ev if x["issue"]})),
                "dataSources": ",".join(sorted({x["dataSource"] for x in ev if x["dataSource"]})),
            }
        )

    php_lines = [
        "<?php",
        "/**",
        " * Plugin Name: Allwaa Merchant Legacy 404 Redirects",
        " * Description: Exact legacy product redirects generated from Merchant LIVE_HTTP_ERROR mappings.",
        " * Version: 2026.08.14",
        " * Author: Allwaa Alakhdar",
        " *",
        " * SAFETY:",
        " * - Exact paths only; no regex.",
        " * - GET/HEAD only.",
        " * - Same-site targets only.",
        " * - Query strings are intentionally ignored when matching an already-404 legacy path.",
        " * - Rollback: remove this one MU-plugin file.",
        " */",
        "",
        "defined('ABSPATH') || exit;",
        "",
        "function allwaa_merchant_legacy404_redirect_map(): array {",
        "    return [",
    ]
    for old_path, new_path in sorted(unique_map.items()):
        php_lines.append(f"        {php_quote(old_path)} => {php_quote(new_path)},")
    php_lines += [
        "    ];",
        "}",
        "",
        "add_action('template_redirect', static function (): void {",
        "    if (is_admin()) {",
        "        return;",
        "    }",
        "",
        "    $method = strtoupper($_SERVER['REQUEST_METHOD'] ?? 'GET');",
        "    if (!in_array($method, ['GET', 'HEAD'], true)) {",
        "        return;",
        "    }",
        "",
        "    $request_uri = (string) ($_SERVER['REQUEST_URI'] ?? '/');",
        "    $path = wp_parse_url($request_uri, PHP_URL_PATH);",
        "    if (!is_string($path) || $path === '') {",
        "        return;",
        "    }",
        "",
        "    $path = '/' . ltrim($path, '/');",
        "    if ($path !== '/' && substr($path, -1) !== '/') {",
        "        $path .= '/';",
        "    }",
        "",
        "    $map = allwaa_merchant_legacy404_redirect_map();",
        "    if (!isset($map[$path])) {",
        "        return;",
        "    }",
        "",
        "    $target_path = $map[$path];",
        "    $target = home_url($target_path);",
        "",
        "    // Exact permanent redirect for a verified legacy 404 only.",
        "    wp_safe_redirect($target, 301, 'Allwaa Merchant Legacy 404');",
        "    exit;",
        "}, 0);",
        "",
    ]

    DATA.mkdir(parents=True, exist_ok=True)
    PHP_OUT.write_text("\n".join(php_lines), encoding="utf-8")

    payload = {
        "timestamp": now_iso(),
        "source": str(INPUT.relative_to(ROOT)),
        "sourceRows": len(rows),
        "eligibleEvidenceRows": len(eligible),
        "uniqueExactRedirects": len(unique_map),
        "rejected": rejected,
        "conflicts": conflicts,
        "redirects": manifest_rows,
        "phpOutput": str(PHP_OUT.relative_to(ROOT)),
        "rollback": "Remove wp-content/mu-plugins/allwaa-merchant-legacy404-redirects.php",
    }
    JSON_OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    fieldnames = ["oldPath", "newPath", "offerIds", "productIds", "issues", "dataSources"]
    with CSV_OUT.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(manifest_rows)

    print("Eligible evidence rows :", len(eligible))
    print("Unique exact redirects :", len(unique_map))
    print("Rejected               :", len(rejected))
    print("Conflicts              :", len(conflicts))
    print("Generated:")
    print(" -", PHP_OUT.relative_to(ROOT))
    print(" -", JSON_OUT.relative_to(ROOT))
    print(" -", CSV_OUT.relative_to(ROOT))
    print("\n✅ PACK BUILT — SITE NOT MODIFIED")

    if rejected:
        print("\n⚠️ Rejected rows:")
        for r in rejected[:50]:
            print(" ", r)

    return 0 if not rejected and not conflicts else 4


if __name__ == "__main__":
    raise SystemExit(main())
