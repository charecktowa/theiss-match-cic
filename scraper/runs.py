"""Locate capture runs and check that one is complete before it is used."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

RUNS_DIR = Path(__file__).resolve().parent / "data" / "cic_raw" / "runs"


def latest_run(runs_dir: Path = RUNS_DIR) -> Path:
    """The newest run that finished, so a new capture is used without --run-dir."""
    finished = [
        path.parent
        for path in sorted(runs_dir.glob("*/run.json"))
        if json.loads(path.read_text(encoding="utf-8")).get("completed_at")
    ]
    if not finished:
        raise SystemExit(f"No finished capture run under {runs_dir}; pass --run-dir.")
    return finished[-1]


def capture_problems(run: dict[str, Any], records: list[dict[str, Any]]) -> list[str]:
    """Why a run cannot be trusted as complete; empty when it can.

    A request counts as failed only if no record for it archived a body: a
    failure retried later (appended to the same manifest) is resolved.
    """
    problems = []
    if not run.get("completed_at"):
        problems.append("the capture did not finish (run.json has no completed_at)")
    archived = {record.get("request_key") for record in records if record.get("path")}
    failed = [
        record
        for record in records
        if not record.get("path") and record.get("request_key") not in archived
    ]
    if failed:
        examples = ", ".join(
            f"{record.get('method')} {record.get('url')} {record.get('form') or ''}".strip()
            for record in failed[:3]
        )
        problems.append(f"{len(failed)} request(s) never archived a response, e.g. {examples}")
    return problems
