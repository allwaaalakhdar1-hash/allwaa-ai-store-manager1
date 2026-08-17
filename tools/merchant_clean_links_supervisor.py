#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
HISTORY_DIR = ROOT / "data" / "agent_history"
LATEST_STATE = ROOT / "data" / "merchant_clean_links_supervisor_latest.json"
ROLLOUT_SCRIPT = ROOT / "tools" / "merchant_clean_links_rollout.py"
FEED_SCOPE_SCRIPT = ROOT / "tools" / "merchant_feed_double_encoding_scope.py"
FEED_SCOPE_JSON = ROOT / "data" / "merchant_feed_double_encoding_scope.json"
PYTHON = ROOT / ".venv" / "bin" / "python"

MERCHANT_ID_DEFAULT = os.getenv("GOOGLE_MERCHANT_ID", "5580031112")
GCC_COUNTRIES = {"OM", "AE", "SA", "KW", "BH", "QA"}
TARGET_ISSUES = {
    "landing_page_crawling_not_allowed",
    "landing_page_error",
    "landing_page_pending_crawl",
    "image_link_pending_crawl",
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat()


def active_rollouts() -> list[dict[str, Any]]:
    result = subprocess.run(
        ["ps", "-eo", "pid=,args="],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    matches: list[dict[str, Any]] = []
    for raw in result.stdout.splitlines():
        line = raw.strip()
        if not line or "merchant_clean_links_rollout.py" not in line:
            continue
        if "merchant_clean_links_supervisor.py" in line:
            continue
        parts = line.split(maxsplit=1)
        if not parts:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        args = parts[1] if len(parts) > 1 else ""
        matches.append({"pid": pid, "args": args})
    return matches


def recent_rollout_activity() -> tuple[str | None, float | None]:
    candidates: list[Path] = []
    patterns = [
        "merchant_clean_links*",
        "*clean_link*rollout*",
        "*merchant*link*rollout*",
    ]
    for base in (ROOT / "data", HISTORY_DIR):
        if not base.exists():
            continue
        for pattern in patterns:
            candidates.extend(p for p in base.glob(pattern) if p.is_file())
    if not candidates:
        return None, None
    latest = max(candidates, key=lambda p: p.stat().st_mtime)
    age_hours = max(0.0, (utc_now().timestamp() - latest.stat().st_mtime) / 3600.0)
    try:
        label = str(latest.relative_to(ROOT))
    except ValueError:
        label = str(latest)
    return label, age_hours


def run_feed_integrity_guard() -> dict[str, Any]:
    """Run the live XML double-encoding audit before any Merchant write decision."""
    if not FEED_SCOPE_SCRIPT.exists():
        return {"ok": False, "error": f"Missing {FEED_SCOPE_SCRIPT}"}
    if not PYTHON.exists():
        return {"ok": False, "error": f"Missing {PYTHON}"}

    env = dict(os.environ)
    src = str(ROOT / "src")
    env["PYTHONPATH"] = src if not env.get("PYTHONPATH") else f"{src}:{env['PYTHONPATH']}"

    proc = subprocess.run(
        [str(PYTHON), str(FEED_SCOPE_SCRIPT)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=240,
    )
    if proc.returncode != 0:
        return {
            "ok": False,
            "returncode": proc.returncode,
            "stdoutTail": proc.stdout[-2000:],
            "stderrTail": proc.stderr[-2000:],
        }
    if not FEED_SCOPE_JSON.exists():
        return {"ok": False, "error": f"Audit did not produce {FEED_SCOPE_JSON}"}

    try:
        payload = json.loads(FEED_SCOPE_JSON.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"ok": False, "error": f"Cannot parse feed audit JSON: {exc!r}"}

    summary = payload.get("summary", {}) or {}
    by_source = payload.get("bySource", {}) or {}
    results = payload.get("results", []) or []
    double_count = int(summary.get("double_encoded_links", 0) or 0)

    bad_ids_by_source: dict[str, set[str]] = {}
    for row in results:
        source = str(row.get("dataSource") or "")
        offer_id = str(row.get("offerId") or "")
        if not source or not offer_id:
            continue
        bad_ids_by_source.setdefault(source, set()).add(offer_id)

    source_names = sorted(bad_ids_by_source)
    shared_ids: set[str] = set()
    if source_names:
        shared_ids = set(bad_ids_by_source[source_names[0]])
        for name in source_names[1:]:
            shared_ids &= bad_ids_by_source[name]

    return {
        "ok": True,
        "doubleEncodedLinks": double_count,
        "itemsWithLink": int(summary.get("items_with_link", 0) or 0),
        "knownCurrentMerchantBad": int(summary.get("KNOWN_CURRENT_MERCHANT_BAD", 0) or 0),
        "latentDoubleEncoded": int(summary.get("LATENT_DOUBLE_ENCODED_IN_FEED", 0) or 0),
        "bySource": by_source,
        "badUniqueIdsBySource": {k: len(v) for k, v in bad_ids_by_source.items()},
        "sharedBadIds": len(shared_ids),
        "sharedBadIdSample": sorted(shared_ids)[:20],
        "scopeJson": str(FEED_SCOPE_JSON.relative_to(ROOT)),
    }


def fetch_merchant_aggregate(merchant_id: str) -> dict[str, Any]:
    """Read-only Merchant API aggregate status snapshot."""
    try:
        import google.auth  # type: ignore
        from google.auth.transport.requests import AuthorizedSession  # type: ignore
    except Exception as exc:
        return {"ok": False, "error": f"google-auth unavailable: {exc!r}"}

    scopes = ["https://www.googleapis.com/auth/content"]
    try:
        credentials, _ = google.auth.default(scopes=scopes)
        session = AuthorizedSession(credentials)
        base = (
            "https://merchantapi.googleapis.com/"
            f"accounts/v1/accounts/{merchant_id}/aggregateProductStatuses"
        )
        page_token: str | None = None
        rows: list[dict[str, Any]] = []
        pages = 0
        while True:
            params = {"pageSize": 1000}
            if page_token:
                params["pageToken"] = page_token
            response = session.get(base, params=params, timeout=60)
            if response.status_code != 200:
                return {
                    "ok": False,
                    "error": f"Merchant API HTTP {response.status_code}",
                    "body": response.text[:1000],
                }
            payload = response.json()
            pages += 1
            for status in payload.get("aggregateProductStatuses", []):
                country = str(status.get("country", "")).upper()
                context = str(status.get("reportingContext", ""))
                if country not in GCC_COUNTRIES:
                    continue
                if context not in {"SHOPPING_ADS", "FREE_LISTINGS"}:
                    continue
                for issue in status.get("itemLevelIssues", []):
                    code = str(issue.get("code", ""))
                    if code not in TARGET_ISSUES:
                        continue
                    rows.append(
                        {
                            "country": country,
                            "reportingContext": context,
                            "code": code,
                            "severity": issue.get("severity"),
                            "numProducts": int(issue.get("numProducts", 0) or 0),
                        }
                    )
            page_token = payload.get("nextPageToken")
            if not page_token:
                break
            if pages >= 20:
                return {"ok": False, "error": "Merchant API pagination safety limit reached"}

        max_by_code: dict[str, int] = {code: 0 for code in TARGET_ISSUES}
        for row in rows:
            max_by_code[row["code"]] = max(max_by_code[row["code"]], row["numProducts"])
        return {
            "ok": True,
            "pages": pages,
            "rows": rows,
            "maxNumProductsByIssue": max_by_code,
            "remainingTargetIssueMax": sum(max_by_code.values()),
        }
    except Exception as exc:
        return {"ok": False, "error": repr(exc)}


def start_rollout() -> dict[str, Any]:
    if not ROLLOUT_SCRIPT.exists():
        return {"started": False, "error": f"Missing {ROLLOUT_SCRIPT}"}
    if not PYTHON.exists():
        return {"started": False, "error": f"Missing {PYTHON}"}

    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    stamp = utc_now().strftime("%Y%m%d_%H%M%S")
    log_path = HISTORY_DIR / f"merchant_clean_links_rollout_resume_{stamp}.log"
    cmd = [
        str(PYTHON), str(ROLLOUT_SCRIPT),
        "--auto", "--batch-size", "100", "--wait-seconds", "300", "--verify-retries", "12",
    ]
    with log_path.open("ab", buffering=0) as log_handle:
        proc = subprocess.Popen(
            cmd,
            cwd=ROOT,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    return {
        "started": True,
        "pid": proc.pid,
        "command": cmd,
        "log": str(log_path.relative_to(ROOT)),
    }


def write_state(state: dict[str, Any]) -> Path:
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    LATEST_STATE.parent.mkdir(parents=True, exist_ok=True)
    stamp = utc_now().strftime("%Y%m%d_%H%M%S")
    history_path = HISTORY_DIR / f"merchant_clean_links_supervisor_{stamp}.json"
    rendered = json.dumps(state, ensure_ascii=False, indent=2)
    history_path.write_text(rendered, encoding="utf-8")
    LATEST_STATE.write_text(rendered, encoding="utf-8")
    return history_path


def finish(state: dict[str, Any]) -> int:
    path = write_state(state)
    print(json.dumps(state, ensure_ascii=False, indent=2))
    print(f"\nSaved: {path.relative_to(ROOT)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Safely supervise the Allwaa Merchant clean-link/feed health workflow."
    )
    parser.add_argument("--merchant-id", default=MERCHANT_ID_DEFAULT)
    parser.add_argument(
        "--resume-if-stopped",
        action="store_true",
        help="Resume the existing clean-link rollout only after every safety gate passes.",
    )
    parser.add_argument(
        "--cooldown-hours",
        type=float,
        default=6.0,
        help="Minimum time to allow Google processing before a stopped rollout is restarted.",
    )
    args = parser.parse_args()

    state: dict[str, Any] = {
        "timestamp": iso_now(),
        "task": "MERCHANT_CLEAN_LINK_ROLLOUT_SUPERVISOR",
        "readOnlyChecks": True,
        "merchantId": args.merchant_id,
        "feedAcceptance": {
            "doubleEncodedLinks": 0,
            "latentDoubleEncoded": 0,
            "rule": "Never declare URL repair complete while the generated XML still contains double-encoded variation links.",
        },
        "verifiedCheckpoint": {
            "source8": {"items": 1868, "links": 1868, "doubleEncoded": 49},
            "source9": {"items": 1868, "links": 1868, "doubleEncoded": 49},
            "sharedBadProductIds": 49,
            "sameExactBadLinks": 0,
            "interpretation": "One shared 49-ID defect rendered differently by two feed sources; likely shared feed-generation/encoding layer, not 98 independent products.",
        },
    }

    running = active_rollouts()
    state["activeRolloutProcesses"] = running

    # Feed XML truth is a hard safety gate. Merchant UI/API may lag behind regeneration.
    feed_guard = run_feed_integrity_guard()
    state["feedIntegrityGuard"] = feed_guard

    if not feed_guard.get("ok"):
        state["decision"] = {
            "health": "FEED_INTEGRITY_UNKNOWN_STOP_WRITES",
            "actions": [
                {
                    "priority": 1,
                    "action": "RESTORE_LIVE_FEED_AUDIT",
                    "reason": "The supervisor could not verify the current generated XML; no Merchant/link write should start without feed truth.",
                }
            ],
        }
        return finish(state)

    if int(feed_guard.get("doubleEncodedLinks", 0) or 0) > 0:
        state["decision"] = {
            "health": "FEED_GENERATOR_REGRESSION_DETECTED",
            "actions": [
                {
                    "priority": 1,
                    "action": "DIAGNOSE_SHARED_FEED_URL_GENERATOR",
                    "reason": (
                        f"Current generated XML still contains {feed_guard.get('doubleEncodedLinks')} double-encoded links. "
                        "Do not hide this with redirects or another clean-link write wave."
                    ),
                },
                {
                    "priority": 2,
                    "action": "RUN_SOURCE_PAIR_CLASSIFICATION",
                    "reason": "Classify the same affected offer IDs across both feeds and fix only the shared encoding/generation layer.",
                    "command": "PYTHONPATH=src .venv/bin/python tools/merchant_feed_source_pair_compare.py",
                },
                {
                    "priority": 3,
                    "action": "KEEP_WOOCOMMERCE_ROUTES_UNCHANGED",
                    "reason": "Current product/canonical routes were previously verified healthy; variation query semantics must be preserved.",
                },
            ],
        }
        return finish(state)

    if int(feed_guard.get("latentDoubleEncoded", 0) or 0) > 0:
        state["decision"] = {
            "health": "LATENT_FEED_ENCODING_DEFECT_STOP_WRITES",
            "actions": [
                {
                    "priority": 1,
                    "action": "FIX_LATENT_FEED_ENCODING",
                    "reason": "Merchant may not yet report every malformed feed URL, but the XML itself still fails acceptance.",
                }
            ],
        }
        return finish(state)

    if running:
        state["decision"] = {
            "health": "RUNNING_SAFE_OBSERVE",
            "actions": [
                {
                    "priority": 1,
                    "action": "MONITOR_CLEAN_LINK_ROLLOUT",
                    "reason": "Existing Merchant write rollout is active; do not start another write operation.",
                },
                {
                    "priority": 2,
                    "action": "CONTINUE_READ_ONLY_MONITORING",
                    "reason": "Generated XML currently passes the feed encoding gate.",
                },
            ],
        }
        state["merchantAggregate"] = fetch_merchant_aggregate(args.merchant_id)
        return finish(state)

    merchant = fetch_merchant_aggregate(args.merchant_id)
    state["merchantAggregate"] = merchant
    last_activity, age_hours = recent_rollout_activity()
    state["lastRolloutActivityFile"] = last_activity
    state["lastRolloutActivityAgeHours"] = age_hours

    if not merchant.get("ok"):
        state["decision"] = {
            "health": "OBSERVE_ONLY_MERCHANT_UNAVAILABLE",
            "actions": [
                {
                    "priority": 1,
                    "action": "RESTORE_MERCHANT_READ_ACCESS",
                    "reason": "Feed XML passes, but a fresh Merchant status snapshot is required before any further write decision.",
                }
            ],
        }
    else:
        remaining = int(merchant.get("remainingTargetIssueMax", 0) or 0)
        state["remainingTargetIssueMax"] = remaining
        if remaining == 0:
            state["decision"] = {
                "health": "CLEAN_LINK_ROLLOUT_COMPLETE",
                "actions": [
                    {
                        "priority": 1,
                        "action": "DO_NOT_RESTART_ROLLOUT",
                        "reason": "Generated XML passes and no target landing-page/link crawl issues remain in the fresh GCC aggregate snapshot.",
                    },
                    {
                        "priority": 2,
                        "action": "CONTINUE_GOOGLE_REPROCESSING_MONITOR",
                        "reason": "Allow Merchant Center to finish propagating approvals without another write wave.",
                    },
                ],
            }
        elif age_hours is not None and age_hours < args.cooldown_hours:
            state["decision"] = {
                "health": "WAIT_FOR_GOOGLE_REPROCESSING",
                "actions": [
                    {
                        "priority": 1,
                        "action": "DO_NOT_RESTART_YET",
                        "reason": (
                            f"Target issues remain, but rollout activity was only {age_hours:.2f}h ago; "
                            f"cooldown is {args.cooldown_hours:.2f}h."
                        ),
                    }
                ],
            }
        elif args.resume_if_stopped:
            start = start_rollout()
            state["resume"] = start
            if start.get("started"):
                state["decision"] = {
                    "health": "RESUMED_CLEAN_LINK_ROLLOUT",
                    "actions": [
                        {
                            "priority": 1,
                            "action": "MONITOR_CLEAN_LINK_ROLLOUT",
                            "reason": "All feed safety gates passed; target Merchant issues remain after cooldown; existing approved rollout resumed.",
                        }
                    ],
                }
            else:
                state["decision"] = {
                    "health": "STOPPED_NEEDS_REVIEW",
                    "actions": [
                        {
                            "priority": 1,
                            "action": "REVIEW_ROLLOUT_START_FAILURE",
                            "reason": start.get("error", "Unknown rollout start failure"),
                        }
                    ],
                }
        else:
            state["decision"] = {
                "health": "READY_TO_RESUME_CLEAN_LINK_ROLLOUT",
                "actions": [
                    {
                        "priority": 1,
                        "action": "RESUME_EXISTING_CLEAN_LINK_ROLLOUT",
                        "reason": "Feed XML passes; no rollout process is active; target Merchant issues remain after the safety cooldown.",
                    }
                ],
            }

    return finish(state)


if __name__ == "__main__":
    raise SystemExit(main())
