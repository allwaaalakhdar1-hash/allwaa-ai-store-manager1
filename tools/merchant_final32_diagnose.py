#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CSV = ROOT / "data" / "merchant_unique_issues.csv"
DEFAULT_STATE = ROOT / "data" / "merchant_clean_links_rollout_state.json"
OUT_JSON = ROOT / "data" / "merchant_final32_diagnose.json"
OUT_CSV = ROOT / "data" / "merchant_final32_diagnose.csv"

TARGET_CODES = {
    "landing_page_crawling_not_allowed",
    "landing_page_error",
    "landing_page_pending_crawl",
    "image_link_pending_crawl",
}

PRIMARY_SOURCES = {
    "accounts/5580031112/dataSources/10647463497",
    "accounts/5580031112/dataSources/10647463716",
}

TRACKING_KEYS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "utm_id",
    "gclid",
    "gbraid",
    "wbraid",
}

UA_NORMAL = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0 Safari/537.36"
)
UA_GOOGLEBOT = (
    "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
)

CANONICAL_RE = re.compile(
    r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\']+)["\']',
    re.I,
)
CANONICAL_RE_REV = re.compile(
    r'<link[^>]+href=["\']([^"\']+)["\'][^>]+rel=["\']canonical["\']',
    re.I,
)
ROBOTS_RE = re.compile(
    r'<meta[^>]+name=["\']robots["\'][^>]+content=["\']([^"\']+)["\']',
    re.I,
)
ROBOTS_RE_REV = re.compile(
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']robots["\']',
    re.I,
)
HTML_LANG_RE = re.compile(r'<html[^>]+lang=["\']([^"\']+)["\']', re.I)


def norm_url(url: str) -> str:
    if not url:
        return ""
    p = urlsplit(url)
    path = p.path or "/"
    if not path.endswith("/") and "." not in path.rsplit("/", 1)[-1]:
        path += "/"
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), path, p.query, ""))


def strip_tracking(url: str) -> str:
    p = urlsplit(url)
    pairs = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
             if k.lower() not in TRACKING_KEYS]
    return urlunsplit((p.scheme, p.netloc, p.path, urlencode(pairs, doseq=True), ""))


def query_profile(url: str) -> dict[str, Any]:
    pairs = parse_qsl(urlsplit(url).query, keep_blank_values=True)
    tracking = [(k, v) for k, v in pairs if k.lower() in TRACKING_KEYS]
    non_tracking = [(k, v) for k, v in pairs if k.lower() not in TRACKING_KEYS]
    return {
        "all": pairs,
        "tracking": tracking,
        "nonTracking": non_tracking,
        "trackingOnly": bool(pairs) and not non_tracking,
        "hasQuery": bool(pairs),
    }


def extract_meta(html: str) -> tuple[str, str, str]:
    canonical = ""
    m = CANONICAL_RE.search(html) or CANONICAL_RE_REV.search(html)
    if m:
        canonical = m.group(1).strip()
    robots = ""
    m = ROBOTS_RE.search(html) or ROBOTS_RE_REV.search(html)
    if m:
        robots = m.group(1).strip()
    lang = ""
    m = HTML_LANG_RE.search(html)
    if m:
        lang = m.group(1).strip()
    return canonical, robots, lang


def request_url(session: requests.Session, url: str, ua: str) -> dict[str, Any]:
    try:
        r = session.get(
            url,
            headers={"User-Agent": ua, "Accept": "text/html,application/xhtml+xml"},
            timeout=30,
            allow_redirects=True,
        )
        canonical, robots, lang = extract_meta(r.text[:1_500_000])
        return {
            "ok": True,
            "status": r.status_code,
            "finalUrl": r.url,
            "redirects": [
                {"status": h.status_code, "url": h.url, "location": h.headers.get("Location", "")}
                for h in r.history
            ],
            "canonical": canonical,
            "robots": robots,
            "htmlLang": lang,
            "contentType": r.headers.get("Content-Type", ""),
            "cache": r.headers.get("X-LiteSpeed-Cache", ""),
        }
    except Exception as exc:
        return {"ok": False, "error": repr(exc)}


def scalar_matches(value: Any, target: str) -> bool:
    if isinstance(value, (str, int)):
        return str(value) == target
    return False


def find_state_paths(obj: Any, target: str, path: str = "$", out: list[str] | None = None) -> list[str]:
    if out is None:
        out = []
    if len(out) >= 30:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            child = f"{path}.{k}"
            if str(k) == target:
                out.append(child + "[key]")
            if scalar_matches(v, target):
                out.append(child)
            else:
                find_state_paths(v, target, child, out)
            if len(out) >= 30:
                break
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            child = f"{path}[{i}]"
            if scalar_matches(v, target):
                out.append(child)
            else:
                find_state_paths(v, target, child, out)
            if len(out) >= 30:
                break
    return out


def load_state(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def parse_issue_rows(path: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        for raw in reader:
            if len(raw) < 12:
                continue
            if raw[0] == "classification" or raw[2] == "code":
                continue
            code = raw[2].strip()
            if code not in TARGET_CODES:
                continue
            rows.append({
                "classification": raw[0].strip(),
                "severity": raw[1].strip(),
                "code": code,
                "reportingContext": raw[3].strip(),
                "countries": raw[4].strip(),
                "impacted": raw[5].strip(),
                "offerId": raw[6].strip(),
                "language": raw[7].strip(),
                "feedLabel": raw[8].strip(),
                "title": raw[9].strip(),
                "link": raw[10].strip(),
                "dataSource": raw[11].strip(),
                "issueTitle": raw[12].strip() if len(raw) > 12 else "",
                "issueDetail": raw[13].strip() if len(raw) > 13 else "",
                "helpUrl": raw[14].strip() if len(raw) > 14 else "",
            })
    return rows


def dedupe(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for r in rows:
        key = (r["code"], r["offerId"], r["language"], r["dataSource"])
        if key not in grouped:
            grouped[key] = {**r, "contexts": [], "countriesSeen": set()}
        grouped[key]["contexts"].append(r["reportingContext"])
        grouped[key]["countriesSeen"].update(
            c.strip() for c in r["countries"].split(",") if c.strip()
        )
    out = []
    for item in grouped.values():
        item["contexts"] = sorted(set(item["contexts"]))
        item["countriesSeen"] = sorted(item["countriesSeen"])
        out.append(item)
    out.sort(key=lambda x: (x["code"], x["offerId"], x["language"]))
    return out


def classify(item: dict[str, Any]) -> tuple[str, list[str]]:
    normal = item["normal"]
    bot = item["googlebot"]
    qp = item["queryProfile"]
    reasons: list[str] = []

    if not normal.get("ok") or not bot.get("ok"):
        reasons.append("request_exception")
        return "LIVE_REQUEST_ERROR", reasons

    ns = int(normal.get("status", 0) or 0)
    bs = int(bot.get("status", 0) or 0)
    if ns >= 400:
        reasons.append(f"normal_http_{ns}")
    if bs >= 400:
        reasons.append(f"googlebot_http_{bs}")
    if ns == 200 and bs >= 400:
        reasons.append("googlebot_blocked_but_browser_ok")
        return "GOOGLEBOT_BLOCKED", reasons
    if ns >= 400 or bs >= 400:
        return "LIVE_HTTP_ERROR", reasons

    if ns != 200 or bs != 200:
        reasons.append(f"unexpected_status_normal_{ns}_bot_{bs}")
        return "HTTP_REVIEW", reasons

    final_n = normal.get("finalUrl", "")
    final_b = bot.get("finalUrl", "")
    if norm_url(final_n).split("?", 1)[0] != norm_url(final_b).split("?", 1)[0]:
        reasons.append("browser_googlebot_final_url_differs")
        return "GOOGLEBOT_ROUTE_DIFF", reasons

    robots = (bot.get("robots") or normal.get("robots") or "").lower()
    if "noindex" in robots or "none" in robots:
        reasons.append(f"robots_meta={robots}")
        return "ROBOTS_NOINDEX", reasons

    canonical = bot.get("canonical") or normal.get("canonical") or ""
    if canonical:
        merchant_clean = strip_tracking(item["link"])
        final_clean = strip_tracking(final_b)
        can_path = norm_url(canonical).split("?", 1)[0]
        final_path = norm_url(final_clean).split("?", 1)[0]
        merch_path = norm_url(merchant_clean).split("?", 1)[0]
        if can_path not in {final_path, merch_path}:
            reasons.append(f"canonical_mismatch={canonical}")
            return "CANONICAL_MISMATCH", reasons

    if qp["nonTracking"]:
        reasons.append("non_tracking_query_present")
        reasons.append(
            "non_tracking_keys=" + ",".join(k for k, _ in qp["nonTracking"])
        )
        return "QUERY_PARAMETER_REVIEW", reasons

    if item["dataSource"] not in PRIMARY_SOURCES:
        reasons.append("non_primary_data_source")
        reasons.append("source=" + item["dataSource"])
        return "DATA_SOURCE_REVIEW", reasons

    if qp["trackingOnly"]:
        reasons.append("tracking_query_only")

    if normal.get("redirects") or bot.get("redirects"):
        reasons.append("redirect_chain_present_but_final_200")

    reasons.append("live_200_googlebot_200_indexable_route")
    return "GOOGLE_STALE_PROCESSING", reasons


def main() -> int:
    parser = argparse.ArgumentParser(description="Diagnose only the remaining Merchant landing-page/link issues.")
    parser.add_argument("--issues-csv", default=str(DEFAULT_CSV))
    parser.add_argument("--state-file", default=str(DEFAULT_STATE))
    args = parser.parse_args()

    issues_path = Path(args.issues_csv)
    state_path = Path(args.state_file)
    if not issues_path.exists():
        raise SystemExit(f"Missing issues CSV: {issues_path}")

    raw = parse_issue_rows(issues_path)
    items = dedupe(raw)
    state = load_state(state_path) if state_path.exists() else None

    print("=" * 110)
    print("ALLWAA MERCHANT — FINAL REMAINING LINK DIAGNOSTIC — READ ONLY")
    print("=" * 110)
    print("Raw issue rows        :", len(raw))
    print("Unique offer issues   :", len(items))
    print("State file            :", state_path if state_path.exists() else "missing")

    session = requests.Session()
    results: list[dict[str, Any]] = []
    class_counts: Counter[str] = Counter()
    code_counts: Counter[str] = Counter()

    for i, item in enumerate(items, 1):
        item = dict(item)
        item["queryProfile"] = query_profile(item["link"])
        item["cleanTrackingSuggestion"] = strip_tracking(item["link"])
        item["normal"] = request_url(session, item["link"], UA_NORMAL)
        item["googlebot"] = request_url(session, item["link"], UA_GOOGLEBOT)
        item["statePaths"] = find_state_paths(state, item["offerId"]) if state is not None else []
        cls, reasons = classify(item)
        item["diagnosis"] = cls
        item["reasons"] = reasons
        results.append(item)
        class_counts[cls] += 1
        code_counts[item["code"]] += 1

        n = item["normal"]
        b = item["googlebot"]
        print(
            f"[{i:02d}/{len(items):02d}] {item['offerId']} | {item['code']} | {cls} | "
            f"normal={n.get('status', 'ERR')} bot={b.get('status', 'ERR')} | {item['link']}"
        )

    summary = {
        "readOnly": True,
        "rawIssueRows": len(raw),
        "uniqueOfferIssues": len(items),
        "byIssueCode": dict(code_counts),
        "byDiagnosis": dict(class_counts),
        "primarySources": sorted(PRIMARY_SOURCES),
        "results": results,
    }

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    fields = [
        "offerId", "language", "code", "diagnosis", "title", "link", "dataSource",
        "normalStatus", "normalFinal", "googlebotStatus", "googlebotFinal", "canonical",
        "robots", "htmlLang", "nonTrackingQuery", "trackingOnly", "statePaths", "reasons",
    ]
    with OUT_CSV.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for item in results:
            writer.writerow({
                "offerId": item["offerId"],
                "language": item["language"],
                "code": item["code"],
                "diagnosis": item["diagnosis"],
                "title": item["title"],
                "link": item["link"],
                "dataSource": item["dataSource"],
                "normalStatus": item["normal"].get("status", ""),
                "normalFinal": item["normal"].get("finalUrl", ""),
                "googlebotStatus": item["googlebot"].get("status", ""),
                "googlebotFinal": item["googlebot"].get("finalUrl", ""),
                "canonical": item["googlebot"].get("canonical") or item["normal"].get("canonical", ""),
                "robots": item["googlebot"].get("robots") or item["normal"].get("robots", ""),
                "htmlLang": item["googlebot"].get("htmlLang") or item["normal"].get("htmlLang", ""),
                "nonTrackingQuery": json.dumps(item["queryProfile"]["nonTracking"], ensure_ascii=False),
                "trackingOnly": item["queryProfile"]["trackingOnly"],
                "statePaths": " | ".join(item["statePaths"]),
                "reasons": " | ".join(item["reasons"]),
            })

    print()
    print("=" * 110)
    print("SUMMARY")
    print("=" * 110)
    print("BY ISSUE CODE")
    for k, v in sorted(code_counts.items()):
        print(f"  {k:40s}: {v}")
    print("BY DIAGNOSIS")
    for k, v in sorted(class_counts.items()):
        print(f"  {k:40s}: {v}")
    print()
    print("Saved:", OUT_JSON.relative_to(ROOT))
    print("Saved:", OUT_CSV.relative_to(ROOT))
    print("✅ READ ONLY — NOTHING MODIFIED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
