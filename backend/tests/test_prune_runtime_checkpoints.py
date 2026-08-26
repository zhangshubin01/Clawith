"""Tests for app/scripts/prune_runtime_checkpoints.py (plan §5-6).

Pure-function coverage for the deletion decision and SQL guardrail assertions
to prevent silent drift of the three safety rails.
"""

from datetime import datetime, timedelta, timezone

from app.scripts import prune_runtime_checkpoints as p

NOW = datetime(2026, 8, 26, 12, 0, 0, tzinfo=timezone.utc)


def _rows(*pairs):
    """Newest-first checkpoint rows as (checkpoint_id, ts)."""
    return [(cid, ts) for cid, ts in pairs]


class TestPickDeletions:
    def test_empty_rows(self):
        assert p.pick_deletions([], 3, NOW) == []

    def test_newest_younger_than_min_age_skips_thread(self):
        # Guardrail 2: latest activity < min-age -> nothing is pruned.
        rows = _rows(
            ("c3", NOW - timedelta(days=1)),
            ("c2", NOW - timedelta(days=4)),
            ("c1", NOW - timedelta(days=5)),
        )
        assert p.pick_deletions(rows, 3, NOW) == []

    def test_missing_ts_skips_thread(self):
        rows = _rows(("c2", None), ("c1", NOW - timedelta(days=5)))
        assert p.pick_deletions(rows, 3, NOW) == []

    def test_keeps_last_n_deletes_rest_in_order(self):
        rows = _rows(
            ("c5", NOW - timedelta(days=10)),
            ("c4", NOW - timedelta(days=11)),
            ("c3", NOW - timedelta(days=12)),
            ("c2", NOW - timedelta(days=13)),
            ("c1", NOW - timedelta(days=14)),
        )
        assert p.pick_deletions(rows, 3, NOW) == ["c2", "c1"]

    def test_keep_clamped_to_one(self):
        rows = _rows(
            ("c3", NOW - timedelta(days=10)),
            ("c2", NOW - timedelta(days=11)),
            ("c1", NOW - timedelta(days=12)),
        )
        assert p.pick_deletions(rows, 0, NOW) == ["c2", "c1"]

    def test_keep_at_least_count_deletes_nothing(self):
        rows = _rows(
            ("c2", NOW - timedelta(days=10)),
            ("c1", NOW - timedelta(days=11)),
        )
        assert p.pick_deletions(rows, 3, NOW) == []


class TestSqlGuardrails:
    def test_candidate_threads_excludes_busy_commands(self):
        # Guardrail 1: threads with queued/claimed commands are never touched.
        assert "cmd.status IN ('queued', 'claimed')" in p.CANDIDATE_THREADS_SQL
        assert "busy.runtime_thread_id IS NULL" in p.CANDIDATE_THREADS_SQL

    def test_deletes_scoped_by_thread_and_ids(self):
        for sql in (p.DELETE_WRITES_SQL, p.DELETE_CHECKPOINTS_SQL):
            assert "thread_id = :tid" in sql
            assert "checkpoint_ns = :ns" in sql
            assert "checkpoint_id = ANY(:ids)" in sql

    def test_blob_gc_uses_channel_versions_reachability(self):
        # Blobs are only removed when no surviving checkpoint's
        # channel_versions references their (channel, version).
        assert "NOT EXISTS" in p.GC_BLOBS_SQL
        assert "jsonb_each_text" in p.GC_BLOBS_SQL
        assert "channel_versions" in p.GC_BLOBS_SQL
        assert "kv.key = b.channel" in p.GC_BLOBS_SQL
        assert "kv.value = b.version" in p.GC_BLOBS_SQL

    def test_ordered_checkpoints_sort_newest_first(self):
        assert "ORDER BY ts DESC NULLS LAST, checkpoint_id DESC" in p.ORDERED_CHECKPOINTS_SQL


class TestHuman:
    def test_bytes(self):
        assert p._human(0) == "0 B"
        assert p._human(1023) == "1023 B"

    def test_units(self):
        assert p._human(1024) == "1.0 KB"
        assert p._human(3 * 1024**2) == "3.0 MB"
        assert p._human(2 * 1024**3) == "2.0 GB"
        assert p._human(3 * 1024**4) == "3.0 TB"
