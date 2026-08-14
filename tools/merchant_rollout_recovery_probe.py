#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
DATA = ROOT / "data"
LOG = DATA / "merchant_clean_links_rollout.log"
OUT = DATA / "merchant_rollout_recovery_probe.json"

SUCCESS_PATTERNS = [
    re.compile(r"\b(?:complete|completed|finished|done|all batches|no remaining)\b", re.I),
]
ERROR_PATTERNS = [
    re.compile(r"traceback", re.I),
    re.compile(r"\b(?:fatal|exception|error)\b", re.I),
]
PROGRESS_PATTERNS = [
    re.compile(r"batch", re.I),
    re.compile(r"verified", re.I),
    re.compile(r"remaining", re.I),
    re.compile(r"updated", re.I),
    re.compile(r"processed", re.I),
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def active_rollout_processes() -> list[dict[str, Any]]:
    proc = subprocess.run(
        ["ps", "-eo", "pid=,etimes=,args="],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    rows: list[dict[str, Any]] = []
    for raw in proc.stdout.splitlines():
        line = raw.strip()
        if "merchant_clean_links_rollout.py" not in line:
            continue
        parts = line.split(maxsplit=2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            elapsed = int(parts[1])
        except ValueError:
            continue
        rows.append({"pid": pid, "elapsedSeconds": elapsed, "args": parts[2]})
    return rows


def tail_lines(path: Path, max_lines: int = 250) -> list[str]:
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    return text.splitlines()[-max_lines:]


def classify_log(lines: list[str]) -> dict[str, Any]:
    success_hits: list[str] = []
    error_hits: list[str] = []
    progress_hits: list[str] = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if any(p.search(stripped) for p in SUCCESS_PATTERNS):
            success_hits.append(stripped)
        if any(p.search(stripped) for p in ERROR_PATTERNS):
            error_hits.append(stripped)
        if any(p.search(stripped) for p in PROGRESS_PATTERNS):
            progress_hits.append(stripped)

    # Prefer a clear traceback/error near the end over generic words like "done".
    last_error_pos = -1
    last_success_pos = -1
    for i, line in enumerate(lines):
        if any(p.search(line) for p in ERROR_PATTERNS):
            last_error_pos = i
        if any(p.search(line) for p in SUCCESS_PATTERNS):
            last_success_pos = i

    if last_error_pos > last_success_pos and last_error_pos >= 0:
        state = "STOPPED_WITH_ERROR"
    elif last_success_pos >= 0 and last_success_pos >= last_error_pos:
        state = "LIKELY_COMPLETED"
    elif lines:
        state = "STOPPED_OR_LOG_STALE_UNCLEAR"
    else:
        state = "NO_LOG_FOUND"

    return {
        "state": state,
        "successHits": success_hits[-20:],
        "errorHits": error_hits[-20:],
        "progressHits": progress_hits[-40:],
        "tail": lines[-120:],
    }


def credential_presence() -> dict[str, Any]:
    # Never print credential values or JSON contents.
    env_names = [
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_SERVICE_ACCOUNT_FILE",
        "GOOGLE_SERVICE_ACCOUNT_JSON",
        "MERCHANT_SERVICE_ACCOUNT_FILE",
        "GCP_SERVICE_ACCOUNT_FILE",
        "GOOGLE_CLOUD_PROJECT",
        "GOOGLE_MERCHANT_ID",
        "MERCHANT_ID",
    ]
    env = {name: bool(os.getenv(name)) for name in env_names}

    candidates: list[str] = []
    search_roots = [ROOT, ROOT / ".secrets", ROOT / "secrets", ROOT / "credentials", ROOT / "config"]
    seen: set[Path] = set()
    for base in search_roots:
        if not base.exists() or not base.is_dir():
            continue
        try:
            iterator = base.glob("*.json") if base != ROOT else base.glob("*.json")
        except OSError:
            continue
        for path in iterator:
            try:
                resolved = path.resolve()
            except OSError:
                continue
            if resolved in seen:
                continue
            seen.add(resolved)
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(data, dict) and data.get("type") == "service_account":
                email = str(data.get("client_email", ""))
                candidates.append(
                    f"{path.relative_to(ROOT)} :: service_account :: {email or '<email hidden/absent>'}"
                )
    return {"environmentFlags": env, "localServiceAccountFiles": candidates}


def merchant_reader_candidates() -> list[str]:
    if not TOOLS.exists():
        return []
    names: list[str] = []
    for path in sorted(TOOLS.glob("*.py")):
        lower = path.name.lower()
        if "merchant" not in lower:
            continue
        if any(token in lower for token in ("audit", "readonly", "guardian", "status", "diagnose")):
            names.append(str(path.relative_to(ROOT)))
    return names


def recent_data_files() -> list[dict[str, Any]]:
    if not DATA.exists():
        return []
    rows: list[dict[str, Any]] = []
    for path in DATA.glob("merchant*"):
        if not path.is_file():
            continue
        stat = path.stat()
        rows.append(
            {
                "path": str(path.relative_to(ROOT)),
                "mtime": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
                "size": stat.st_size,
            }
        )
    rows.sort(key=lambda x: x["mtime"], reverse=True)
    return rows[:30]


def main() -> int:
    lines = tail_lines(LOG)
    active = active_rollout_processes()
    log_info = classify_log(lines)

    payload: dict[str, Any] = {
        "timestamp": now_iso(),
        "task": "MERCHANT_ROLLOUT_RECOVERY_PROBE",
        "readOnly": True,
        "activeRolloutProcesses": active,
        "rolloutLog": {
            "path": str(LOG.relative_to(ROOT)),
            "exists": LOG.exists(),
            "mtime": (
                datetime.fromtimestamp(LOG.stat().st_mtime, timezone.utc).isoformat()
                if LOG.exists()
                else None
            ),
            **log_info,
        },
        "merchantReaderCandidates": merchant_reader_candidates(),
        "credentialPresence": credential_presence(),
        "recentMerchantDataFiles": recent_data_files(),
    }

    if active:
        decision = "ACTIVE_DO_NOT_START_SECOND_WRITER"
    elif log_info["state"] == "STOPPED_WITH_ERROR":
        decision = "REVIEW_ERROR_BEFORE_RESUME"
    elif log_info["state"] == "LIKELY_COMPLETED":
        decision = "VERIFY_MERCHANT_READONLY_BEFORE_ANY_RESUME"
    else:
        decision = "VERIFY_MERCHANT_READONLY_AND_LOG_BEFORE_RESUME"

    payload["decision"] = decision

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print("=" * 100)
    print("MERCHANT ROLLOUT RECOVERY PROBE — READ ONLY")
    print("=" * 100)
    print("Active rollout processes :", len(active))
    print("Log state                :", log_info["state"])
    print("Log mtime                :", payload["rolloutLog"]["mtime"])
    print("Merchant readers found   :", len(payload["merchantReaderCandidates"]))
    for item in payload["merchantReaderCandidates"]:
        print("  -", item)
    print("Credential env flags     :")
    for key, is_set in payload["credentialPresence"]["environmentFlags"].items():
        print(f"  {key}: {'SET' if is_set else 'not set'}")
    print("Service-account files    :", len(payload["credentialPresence"]["localServiceAccountFiles"]))
    for item in payload["credentialPresence"]["localServiceAccountFiles"]:
        print("  -", item)
    print("Decision                  :", decision)

    print("\n--- LAST IMPORTANT LOG LINES ---")
    important = (log_info["errorHits"] + log_info["successHits"] + log_info["progressHits"])[-60:]
    if important:
        for line in important:
            print(line)
    else:
        print("No classified important lines found; inspect saved JSON tail.")

    print("\nSaved:", OUT.relative_to(ROOT))
    print("✅ READ ONLY — NOTHING MODIFIED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
