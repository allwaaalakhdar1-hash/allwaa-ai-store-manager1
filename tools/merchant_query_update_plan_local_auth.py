#!/usr/bin/env python3
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account

ROOT = Path(__file__).resolve().parents[1]
SCOPES = ["https://www.googleapis.com/auth/content"]
SOURCE_SCRIPTS = [
    ROOT / "tools" / "merchant_unique_audit.py",
    ROOT / "tools" / "merchant_readonly.py",
]


def _assignment_block(text: str, name: str) -> str:
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if re.match(rf"^\s*{re.escape(name)}\s*=", line):
            block = [line]
            balance = line.count("(") + line.count("[") + line.count("{")
            balance -= line.count(")") + line.count("]") + line.count("}")
            j = i + 1
            while j < len(lines) and balance > 0:
                block.append(lines[j])
                balance += lines[j].count("(") + lines[j].count("[") + lines[j].count("{")
                balance -= lines[j].count(")") + lines[j].count("]") + lines[j].count("}")
                j += 1
            return "\n".join(block)
    return ""


def _literal_strings(block: str) -> list[str]:
    if not block:
        return []
    try:
        tree = ast.parse(block)
    except SyntaxError:
        return re.findall(r"['\"]([^'\"]+)['\"]", block)
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append(node.value)
    return out


def _candidate_paths(script: Path) -> list[Path]:
    try:
        text = script.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    block = _assignment_block(text, "CREDENTIAL_FILE")
    pieces = _literal_strings(block)
    candidates: list[Path] = []

    # Absolute literal path.
    for s in pieces:
        p = Path(s).expanduser()
        if p.is_absolute():
            candidates.append(p)

    # Common ROOT / "dir" / "file.json" pattern.
    relative_pieces = [s for s in pieces if not Path(s).is_absolute()]
    if relative_pieces:
        candidates.append(ROOT.joinpath(*relative_pieces))
        for s in relative_pieces:
            candidates.append(ROOT / s)
            candidates.append(script.parent / s)

    # Last-resort: nearby JSON literals from the credential assignment only.
    for p in list(candidates):
        if p.suffix.lower() == ".json":
            continue

    # Preserve order, remove duplicates.
    unique: list[Path] = []
    seen: set[str] = set()
    for p in candidates:
        key = str(p)
        if key not in seen:
            unique.append(p)
            seen.add(key)
    return unique


def discover_credential_file() -> Path:
    checked = 0
    for script in SOURCE_SCRIPTS:
        for candidate in _candidate_paths(script):
            checked += 1
            if candidate.is_file():
                return candidate
    raise FileNotFoundError(
        f"Could not resolve CREDENTIAL_FILE from local Merchant readers (checked {checked} candidates)."
    )


def make_session():
    credential_file = discover_credential_file()
    credentials = service_account.Credentials.from_service_account_file(
        str(credential_file),
        scopes=SCOPES,
    )
    # Do not print the credential path or any credential contents.
    return AuthorizedSession(credentials), "local-service-account-file"


def main() -> int:
    tools_dir = str(ROOT / "tools")
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)

    import merchant_query_update_plan as plan

    plan.make_session = make_session
    return int(plan.main())


if __name__ == "__main__":
    raise SystemExit(main())
