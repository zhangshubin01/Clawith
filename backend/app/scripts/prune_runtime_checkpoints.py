"""Prune old runtime checkpoints, keeping the last N per thread.

Design: docs/technical-plans/20260819-checkpoint-retention-study.md (§5-6).
The upstream ``AsyncPostgresSaver.prune`` is NotImplementedError, and every
RuntimeGraphState channel is a full-value rewrite (no DeltaChannel), so
cascading row deletion plus (channel, version) reachability GC of blobs is safe.

Safety rails (all enforced here):
  1. Threads with queued/claimed commands are never touched.
  2. Threads whose newest checkpoint is younger than ``--min-age-days`` are skipped.
  3. ``--dry-run`` is the default; deletions require ``--apply``.
  4. ``--keep-per-thread`` is clamped to >= 1; blobs are only removed when their
     (channel, version) is not referenced by any surviving checkpoint's
     ``channel_versions``.

Usage (inside the backend container):
    python -m app.scripts.prune_runtime_checkpoints --dry-run
    python -m app.scripts.prune_runtime_checkpoints --apply --keep-per-thread 3 --min-age-days 3
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from app.database import async_session

CHECKPOINT_SCHEMA = "langgraph_checkpoint"

# --- SQL --------------------------------------------------------------------

CANDIDATE_THREADS_SQL = f"""
SELECT DISTINCT c.thread_id, c.checkpoint_ns
FROM {CHECKPOINT_SCHEMA}.checkpoints c
LEFT JOIN (
    SELECT r.runtime_thread_id
    FROM agent_runs r
    JOIN agent_run_commands cmd ON cmd.run_id = r.id
    WHERE cmd.status IN ('queued', 'claimed')
) busy ON busy.runtime_thread_id = c.thread_id
WHERE busy.runtime_thread_id IS NULL
"""

ORDERED_CHECKPOINTS_SQL = f"""
SELECT checkpoint_id, (checkpoint->>'ts')::timestamptz AS ts
FROM {CHECKPOINT_SCHEMA}.checkpoints
WHERE thread_id = :tid AND checkpoint_ns = :ns
ORDER BY ts DESC NULLS LAST, checkpoint_id DESC
"""

DELETE_WRITES_SQL = f"""
DELETE FROM {CHECKPOINT_SCHEMA}.checkpoint_writes
WHERE thread_id = :tid AND checkpoint_ns = :ns AND checkpoint_id = ANY(:ids)
"""

DELETE_CHECKPOINTS_SQL = f"""
DELETE FROM {CHECKPOINT_SCHEMA}.checkpoints
WHERE thread_id = :tid AND checkpoint_ns = :ns AND checkpoint_id = ANY(:ids)
"""

_BLOB_GC_PREDICATE = f"""
      SELECT 1
      FROM {CHECKPOINT_SCHEMA}.checkpoints c
      JOIN LATERAL jsonb_each_text(c.checkpoint -> 'channel_versions') kv ON true
      WHERE c.thread_id = b.thread_id
        AND c.checkpoint_ns = b.checkpoint_ns
        AND c.checkpoint_id IN (
            SELECT k.checkpoint_id
            FROM (
                SELECT checkpoint_id,
                       row_number() OVER (
                           ORDER BY (checkpoint->>'ts')::timestamptz DESC NULLS LAST,
                                    checkpoint_id DESC
                       ) AS rn
                FROM {CHECKPOINT_SCHEMA}.checkpoints
                WHERE thread_id = :tid AND checkpoint_ns = :ns
            ) k
            WHERE k.rn <= :keep
        )
        AND kv.key = b.channel
        AND kv.value = b.version
"""

GC_BLOBS_SQL = f"""
DELETE FROM {CHECKPOINT_SCHEMA}.checkpoint_blobs b
WHERE b.thread_id = :tid AND b.checkpoint_ns = :ns
  AND NOT EXISTS ({_BLOB_GC_PREDICATE})
"""

GC_BLOBS_STATS_SQL = f"""
SELECT count(*) AS n, COALESCE(sum(octet_length(b.blob)), 0) AS bytes
FROM {CHECKPOINT_SCHEMA}.checkpoint_blobs b
WHERE b.thread_id = :tid AND b.checkpoint_ns = :ns
  AND NOT EXISTS ({_BLOB_GC_PREDICATE})
"""

WRITES_COUNT_SQL = f"""
SELECT count(*) FROM {CHECKPOINT_SCHEMA}.checkpoint_writes
WHERE thread_id = :tid AND checkpoint_ns = :ns AND checkpoint_id = ANY(:ids)
"""


def pick_deletions(
    rows: list[tuple[str, datetime | None]],
    keep: int,
    min_age_dt: datetime,
) -> list[str]:
    """Decide which checkpoint_ids to delete.

    ``rows`` are ordered newest-first. Guardrail 2: if the newest checkpoint is
    missing a ts or is younger than ``min_age_dt``, nothing is deleted.
    """
    if not rows:
        return []
    newest_ts = rows[0][1]
    if newest_ts is None or newest_ts >= min_age_dt:
        return []
    keep = max(1, keep)
    return [checkpoint_id for checkpoint_id, _ in rows[keep:]]


async def _candidate_threads(session):
    result = await session.execute(text(CANDIDATE_THREADS_SQL))
    return [(row[0], row[1]) for row in result.fetchall()]


async def _thread_stats(
    session, tid: str, ns: str, keep: int, min_age_dt: datetime
) -> dict:
    """Read-only statistics for a thread that would be affected."""
    ordered = await session.execute(
        text(ORDERED_CHECKPOINTS_SQL), {"tid": tid, "ns": ns}
    )
    rows = [(r[0], r[1]) for r in ordered.fetchall()]
    delete_ids = pick_deletions(rows, keep, min_age_dt)
    if not delete_ids:
        return {"skipped": True, "checkpoint_count": len(rows)}
    writes_n = (
        await session.execute(
            text(WRITES_COUNT_SQL), {"tid": tid, "ns": ns, "ids": delete_ids}
        )
    ).scalar_one()
    blob_n, blob_bytes = (
        await session.execute(
            text(GC_BLOBS_STATS_SQL), {"tid": tid, "ns": ns, "keep": keep}
        )
    ).fetchone()
    return {
        "skipped": False,
        "checkpoint_count": len(rows),
        "delete_checkpoints": len(delete_ids),
        "delete_writes": writes_n,
        "delete_blobs": blob_n,
        "delete_blob_bytes": int(blob_bytes or 0),
        "delete_ids": delete_ids,
    }


async def _apply_thread(session, tid: str, ns: str, stats: dict, keep: int) -> dict:
    """Delete rows for one thread within a single transaction."""
    ids = stats["delete_ids"]
    writes_del = (
        await session.execute(
            text(DELETE_WRITES_SQL), {"tid": tid, "ns": ns, "ids": ids}
        )
    ).rowcount
    cps_del = (
        await session.execute(
            text(DELETE_CHECKPOINTS_SQL), {"tid": tid, "ns": ns, "ids": ids}
        )
    ).rowcount
    blobs_del = (
        await session.execute(
            text(GC_BLOBS_SQL), {"tid": tid, "ns": ns, "keep": keep}
        )
    ).rowcount
    return {"writes": writes_del, "checkpoints": cps_del, "blobs": blobs_del}


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


async def run(
    dry_run: bool,
    keep: int,
    min_age_dt: datetime,
    limit: int | None,
) -> int:
    totals = {"checkpoints": 0, "writes": 0, "blobs": 0, "bytes": 0}
    touched = 0
    async with async_session() as session:
        threads = await _candidate_threads(session)
        print(f"candidate threads (no busy commands): {len(threads)}")
        for idx, (tid, ns) in enumerate(threads):
            if limit is not None and idx >= limit:
                print(f"... stopped after --limit {limit} threads")
                break
            stats = await _thread_stats(session, tid, ns, keep, min_age_dt)
            if stats["skipped"]:
                continue
            touched += 1
            if dry_run:
                print(
                    f"  thread {tid[:12]}… ns={ns!r}: {stats['checkpoint_count']} cps"
                    f" -> keep, delete {stats['delete_checkpoints']} cps /"
                    f" {stats['delete_writes']} writes / {stats['delete_blobs']}"
                    f" blobs ({_human(stats['delete_blob_bytes'])})"
                )
            else:
                result = await _apply_thread(session, tid, ns, stats, keep)
                await session.commit()
                print(
                    f"  thread {tid[:12]}… ns={ns!r}: deleted {result['checkpoints']}"
                    f" cps / {result['writes']} writes / {result['blobs']} blobs"
                )
            totals["checkpoints"] += stats["delete_checkpoints"]
            totals["writes"] += stats["delete_writes"]
            totals["blobs"] += stats["delete_blobs"]
            totals["bytes"] += stats["delete_blob_bytes"]
    print(
        f"{'DRY-RUN would delete' if dry_run else 'TOTAL deleted'}: "
        f"{totals['checkpoints']} checkpoints, {totals['writes']} writes, "
        f"{totals['blobs']} blobs ({_human(totals['bytes'])}), "
        f"{touched} threads"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", dest="apply", action="store_false", help="report only (default)")
    mode.add_argument("--apply", dest="apply", action="store_true", help="actually delete")
    parser.set_defaults(apply=False)
    parser.add_argument("--keep-per-thread", type=int, default=3)
    parser.add_argument("--min-age-days", type=int, default=3)
    parser.add_argument("--limit", type=int, default=None, help="cap threads processed")
    args = parser.parse_args()
    min_age_dt = datetime.now(timezone.utc) - timedelta(days=max(0, args.min_age_days))
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"=== {mode}: keep {args.keep_per_thread} per thread, min-age {args.min_age_days}d ===")
    return asyncio.run(
        run(dry_run=not args.apply, keep=args.keep_per_thread, min_age_dt=min_age_dt, limit=args.limit)
    )


if __name__ == "__main__":
    raise SystemExit(main())
