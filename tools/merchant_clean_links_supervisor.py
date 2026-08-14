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
                return {
                    "ok": False,
                    "error": "Merchant API pagination safety limit reached",
                }

        max_by_code: dict[str, int] = {code: 0 for code in TARGET_ISSUES}
        for row in rows:
            max_by_code[row["code"]] = max(
                max_by_code[row["code"]], row["numProducts"]
            )
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
        str(PYTHON),
        str(ROLLOUT_SCRIPT),
        "--auto",
        "--batch-size",
        "100",
        "--wait-seconds",
        "300",
        "--verify-retries",
        "12",
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


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Safely supervise/resume the Allwaa Merchant clean-link rollout."
    )
    parser.add_argument("--merchant-id", default=MERCHANT_ID_DEFAULT)
    parser.add_argument(
        "--resume-if-stopped",
        action="store_true",
        help="Resume the existing rollout only after safety gates pass.",
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
        "rolloutScript": str(ROLLOUT_SCRIPT.relative_to(ROOT)),
        "expectedRolloutArgs": [
            "--auto",
            "--batch-size", "100",
            "--wait-seconds", "300",
            "--verify-retries", "12",
        ],
    }

    running = active_rollouts()
    state["activeRolloutProcesses"] = running

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
                    "reason": "Read-only Merchant/Woo/GSC observation is safe while rollout is active.",
                },
            ],
        }
        state["merchantAggregate"] = fetch_merchant_aggregate(args.merchant_id)
        path = write_state(state)
        print(json.dumps(state, ensure_ascii=False, indent=2))
        print(f"\nSaved: {path.relative_to(ROOT)}")
        return 0

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
                    "reason": "Cannot safely decide whether to resume a Merchant write without a fresh read-only Merchant status snapshot.",
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
                        "reason": "No target landing-page/link crawl issues remain in the fresh GCC aggregate snapshot.",
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
                            "reason": "No rollout was active, link issues remain after cooldown, and the existing approved rollout was resumed.",
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
                        "reason": "No rollout process is active and target landing-page/link issues remain after the safety cooldown.",
                    }
                ],
            }

    path = write_state(state)
    print(json.dumps(state, ensure_ascii=False, indent=2))
    print(f"\nSaved: {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
