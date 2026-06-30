"""Polling Lark watcher."""

from __future__ import annotations

import time
from dataclasses import dataclass

from bugpatrol.backfill import BackfillResult, run_lark_backfill
from bugpatrol.config import ProjectConfig
from bugpatrol.intake_workflow import IntakeWorkflow
from bugpatrol.lark import LarkOpenApiMessengerClient


@dataclass(frozen=True)
class WatchResult:
    iterations: int
    scanned: int
    processed: int
    skipped: int


def run_polling_watcher(
    *,
    config: ProjectConfig,
    lark: LarkOpenApiMessengerClient,
    workflow: IntakeWorkflow,
    limit: int = 20,
    interval_seconds: float = 30,
    once: bool = False,
    dry_run: bool = False,
    max_iterations: int | None = None,
) -> WatchResult:
    iterations = 0
    scanned = 0
    processed = 0
    skipped = 0
    while True:
        result = run_lark_backfill(
            config=config,
            lark=lark,
            workflow=workflow,
            limit=limit,
            dry_run=dry_run,
        )
        iterations += 1
        scanned += result.scanned
        processed += result.processed
        skipped += result.skipped
        if once or (max_iterations is not None and iterations >= max_iterations):
            return WatchResult(
                iterations=iterations,
                scanned=scanned,
                processed=processed,
                skipped=skipped,
            )
        time.sleep(interval_seconds)
