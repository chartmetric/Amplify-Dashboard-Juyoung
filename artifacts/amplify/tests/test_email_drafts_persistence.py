"""Tests that lock in the wipe-proof email-drafts persistence invariants.

Regression coverage for the 2026-05-01 incident where a transient blip in
`_load_email_drafts` combined with the destructive
"delete-then-reinsert-the-entire-list" pattern in `_save_email_drafts` could
silently wipe every draft in the table on the next save / mark-published /
mark-downloaded / delete call.

Covers:
  (a) When `_load_email_drafts` raises `EmailDraftsUnavailable`, the save /
      mark-published / mark-downloaded / delete endpoints return 503 and
      the underlying store is unchanged (no destructive write).
  (b) Deleting one of three drafts leaves the other two intact (per-row
      delete, never an inverted "keep these" bulk delete).
  (c) Calling `_save_email_drafts({"drafts": []})` is rejected with a
      `ValueError` and does not wipe the existing rows.
  (d) The eviction-by-cap path removes exactly the intended ids and
      nothing else, and logs a WARNING with that explicit id list.
  (e) The daily snapshot file is created on the first successful load of
      the day and is NOT re-created on subsequent loads.

Tests run against the JSON fallback (no Postgres) for portability with the
existing `test_downloaded_tab.py` setup. Both `_DRAFTS_DB_URL` and
`_drafts_db_conn` are neutralized so the loader picks the JSON path.

Run with:
    cd artifacts/amplify && python -m unittest tests.test_email_drafts_persistence
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_AMPLIFY_DIR = os.path.dirname(_HERE)
if _AMPLIFY_DIR not in sys.path:
    sys.path.insert(0, _AMPLIFY_DIR)

import app as amplify_app  # noqa: E402


def _draft_payload(draft_id: str, name: str = "Demo", subject: str = "s") -> dict:
    return {
        "id": draft_id,
        "name": name,
        "snapshot": {
            "channel": "email_standalone",
            "featureIds": ["f1"],
            "combined": {"subject": subject},
        },
    }


class _JsonFallbackBase(unittest.TestCase):
    """Shared setup: isolate the JSON store and force the JSON fallback."""

    def setUp(self):
        self._patch_url = mock.patch.object(amplify_app, "_DRAFTS_DB_URL", "")
        self._patch_url.start()
        self._patch_conn = mock.patch.object(
            amplify_app, "_drafts_db_conn", return_value=None
        )
        self._patch_conn.start()
        self._tmpdir = tempfile.mkdtemp(prefix="email_drafts_test_")
        self._tmp_path = os.path.join(self._tmpdir, ".email_drafts.json")
        with open(self._tmp_path, "w") as f:
            f.write('{"drafts":[]}')
        self._orig_path = amplify_app._EMAIL_DRAFTS_PATH
        self._orig_cache_dir = amplify_app._FEATURES_CACHE_DIR
        amplify_app._EMAIL_DRAFTS_PATH = self._tmp_path
        # Snapshots land in _FEATURES_CACHE_DIR; point it at the test dir.
        amplify_app._FEATURES_CACHE_DIR = self._tmpdir
        self.client = amplify_app.app.test_client()

    def tearDown(self):
        self._patch_conn.stop()
        self._patch_url.stop()
        amplify_app._EMAIL_DRAFTS_PATH = self._orig_path
        amplify_app._FEATURES_CACHE_DIR = self._orig_cache_dir
        # Best-effort cleanup; tests should leave no garbage behind.
        try:
            for fn in os.listdir(self._tmpdir):
                try:
                    os.unlink(os.path.join(self._tmpdir, fn))
                except OSError:
                    pass
            os.rmdir(self._tmpdir)
        except OSError:
            pass

    def _save_via_route(self, draft_id: str, name: str = "Demo", subject: str = "s"):
        return self.client.post(
            "/api/email-drafts",
            data=json.dumps(_draft_payload(draft_id, name=name, subject=subject)),
            content_type="application/json",
        )

    def _seed_drafts(self, ids):
        """Seed the JSON store directly with the given draft ids."""
        rows = [
            {
                "id": i,
                "name": f"Seed {i}",
                "ts": 1000.0 + idx,
                "status": "draft",
                "last_published_ts": 0,
                "last_recipient_count": 0,
                "snapshot": {
                    "channel": "email_standalone",
                    "featureIds": ["f1"],
                    "combined": {"subject": f"s-{i}"},
                },
                "category": None,
                "downloaded_ts": None,
            }
            for idx, i in enumerate(ids)
        ]
        with open(self._tmp_path, "w") as f:
            json.dump({"drafts": rows}, f)

    def _read_disk_ids(self):
        with open(self._tmp_path, "r") as f:
            data = json.load(f)
        return [d.get("id") for d in (data.get("drafts") or [])]


# ---------------------------------------------------------------------------
# (a) Transient load failure -> 503 + no destructive write
# ---------------------------------------------------------------------------


class LoadFailureReturns503Tests(_JsonFallbackBase):
    """When the loader raises, every write endpoint must refuse + 503."""

    def setUp(self):
        super().setUp()
        # Pre-populate three drafts so we can prove the table is unchanged.
        self._seed_drafts(["alpha111", "bravo222", "charlie3"])
        # Patch `_load_email_drafts` to simulate a transient DB blip.
        self._patch_load = mock.patch.object(
            amplify_app,
            "_load_email_drafts",
            side_effect=amplify_app.EmailDraftsUnavailable("simulated DB blip"),
        )
        self._patch_load.start()

    def tearDown(self):
        self._patch_load.stop()
        super().tearDown()

    def _assert_disk_unchanged(self):
        self.assertEqual(
            sorted(self._read_disk_ids()),
            sorted(["alpha111", "bravo222", "charlie3"]),
        )

    def test_save_returns_503_and_does_not_write(self):
        rv = self._save_via_route("delta444")
        self.assertEqual(rv.status_code, 503, rv.data)
        body = json.loads(rv.data)
        self.assertEqual(body.get("error"), "drafts store temporarily unavailable")
        self._assert_disk_unchanged()

    def test_mark_published_returns_503_and_does_not_write(self):
        rv = self.client.post("/api/email-drafts/alpha111/mark-published")
        self.assertEqual(rv.status_code, 503, rv.data)
        self._assert_disk_unchanged()

    def test_mark_downloaded_returns_503_and_does_not_write(self):
        # mark-downloaded gates on `_DRAFTS_DB_URL`; in this JSON-fallback
        # scenario it goes through the lock-protected load path which now
        # propagates EmailDraftsUnavailable as a 503.
        rv = self.client.post("/api/email-drafts/alpha111/mark-downloaded")
        self.assertEqual(rv.status_code, 503, rv.data)
        self._assert_disk_unchanged()

    def test_delete_returns_503_when_db_unavailable(self):
        # delete uses `_delete_email_draft_by_id` directly; in the JSON
        # path it doesn't call `_load_email_drafts`, but the destructive
        # surface area is per-row so the disk MUST be unchanged either way.
        # We patch `_delete_email_draft_by_id` to raise to mirror the
        # configured-DB outage scenario.
        with mock.patch.object(
            amplify_app,
            "_delete_email_draft_by_id",
            side_effect=amplify_app.EmailDraftsUnavailable("simulated DB blip"),
        ):
            rv = self.client.delete("/api/email-drafts/alpha111")
        self.assertEqual(rv.status_code, 503, rv.data)
        self._assert_disk_unchanged()


# ---------------------------------------------------------------------------
# (b) Deleting one of three leaves the other two intact
# ---------------------------------------------------------------------------


class DeleteOnlyTouchesOneRowTests(_JsonFallbackBase):
    def test_delete_one_of_three_leaves_others_intact(self):
        self._seed_drafts(["aaaa1111", "bbbb2222", "cccc3333"])
        rv = self.client.delete("/api/email-drafts/bbbb2222")
        self.assertEqual(rv.status_code, 200, rv.data)
        remaining = sorted(self._read_disk_ids())
        self.assertEqual(remaining, ["aaaa1111", "cccc3333"])

    def test_delete_unknown_id_returns_404_and_leaves_disk_unchanged(self):
        self._seed_drafts(["aaaa1111", "bbbb2222"])
        rv = self.client.delete("/api/email-drafts/missing9")
        self.assertEqual(rv.status_code, 404)
        self.assertEqual(sorted(self._read_disk_ids()), ["aaaa1111", "bbbb2222"])


# ---------------------------------------------------------------------------
# (c) `_save_email_drafts({"drafts": []})` is rejected, not honored
# ---------------------------------------------------------------------------


class EmptySaveRejectedTests(_JsonFallbackBase):
    def test_save_email_drafts_with_empty_list_raises(self):
        self._seed_drafts(["aaaa1111", "bbbb2222"])
        with self.assertRaises(ValueError):
            amplify_app._save_email_drafts({"drafts": []})
        # Disk untouched.
        self.assertEqual(
            sorted(self._read_disk_ids()),
            ["aaaa1111", "bbbb2222"],
        )

    def test_save_email_drafts_with_missing_drafts_key_raises(self):
        self._seed_drafts(["aaaa1111"])
        with self.assertRaises(ValueError):
            amplify_app._save_email_drafts({})
        with self.assertRaises(ValueError):
            amplify_app._save_email_drafts(None)  # type: ignore[arg-type]
        self.assertEqual(self._read_disk_ids(), ["aaaa1111"])


# ---------------------------------------------------------------------------
# (d) Eviction-by-cap removes exactly the intended ids
# ---------------------------------------------------------------------------


class EvictionExplicitIdsTests(_JsonFallbackBase):
    def test_evict_drops_only_named_ids(self):
        self._seed_drafts(["keep0001", "drop0001", "keep0002", "drop0002"])
        with self.assertLogs(amplify_app.logger, level="WARNING") as cap:
            removed = amplify_app._evict_email_drafts_by_ids(["drop0001", "drop0002"])
        self.assertEqual(removed, 2)
        self.assertEqual(sorted(self._read_disk_ids()), ["keep0001", "keep0002"])
        # Eviction must surface in WARNING-level logs with the explicit
        # id list so a future wipe is immediately visible.
        joined = "\n".join(cap.output)
        self.assertIn("eviction", joined)
        self.assertIn("drop0001", joined)
        self.assertIn("drop0002", joined)

    def test_evict_with_empty_list_is_noop(self):
        self._seed_drafts(["keep0001", "keep0002"])
        self.assertEqual(amplify_app._evict_email_drafts_by_ids([]), 0)
        self.assertEqual(amplify_app._evict_email_drafts_by_ids(None), 0)
        self.assertEqual(sorted(self._read_disk_ids()), ["keep0001", "keep0002"])

    def test_save_eviction_path_only_removes_intended_ids(self):
        # Force the size cap to a tiny value so adding a new draft triggers
        # eviction of the older existing drafts. Each seeded record is
        # roughly ~250 bytes serialized, so a 400-byte cap forces all the
        # seed rows out and leaves just the new one.
        self._seed_drafts(["older001", "middle01", "newer001"])
        with mock.patch.object(amplify_app, "_EMAIL_DRAFTS_TOTAL_MAX_BYTES", 400):
            with self.assertLogs(amplify_app.logger, level="WARNING") as cap:
                rv = self._save_via_route("brand111")
        self.assertEqual(rv.status_code, 200, rv.data)
        ids = sorted(self._read_disk_ids())
        # The newly-saved draft must always survive eviction.
        self.assertIn("brand111", ids)
        # At least one of the seeded drafts should have been evicted.
        seeded = {"older001", "middle01", "newer001"}
        self.assertTrue(
            seeded - set(ids),
            f"expected at least one of {seeded} to be evicted, got {ids}",
        )
        # The eviction WARNING must name the exact ids it dropped, and
        # must NOT name the new draft (which would be a "wipe" footgun).
        eviction_lines = [line for line in cap.output if "eviction" in line]
        self.assertTrue(eviction_lines, f"no eviction WARNING in logs: {cap.output}")
        for line in eviction_lines:
            self.assertNotIn(
                "brand111",
                line,
                f"new draft id appeared in eviction log: {line}",
            )


# ---------------------------------------------------------------------------
# (e) Daily snapshot is written once per day
# ---------------------------------------------------------------------------


class DailySnapshotTests(_JsonFallbackBase):
    def _snapshot_files(self):
        return sorted(
            fn for fn in os.listdir(self._tmpdir)
            if fn.startswith(amplify_app._EMAIL_DRAFTS_SNAPSHOT_PREFIX)
            and fn.endswith(".json")
        )

    def test_snapshot_created_on_first_load_and_not_recreated(self):
        self._seed_drafts(["aaaa1111", "bbbb2222"])
        self.assertEqual(self._snapshot_files(), [])

        # First successful load -> snapshot written.
        amplify_app._load_email_drafts()
        snaps = self._snapshot_files()
        self.assertEqual(len(snaps), 1, snaps)
        snap_path = os.path.join(self._tmpdir, snaps[0])
        first_mtime = os.path.getmtime(snap_path)

        with open(snap_path, "r") as f:
            payload = json.load(f)
        snap_ids = sorted(d.get("id") for d in (payload.get("drafts") or []))
        self.assertEqual(snap_ids, ["aaaa1111", "bbbb2222"])

        # Subsequent loads must NOT rewrite today's snapshot.
        # Sleep a tick so any rewrite would change mtime detectably.
        import time as _time
        _time.sleep(0.05)
        amplify_app._load_email_drafts()
        amplify_app._load_email_drafts()
        snaps_after = self._snapshot_files()
        self.assertEqual(snaps_after, snaps)
        self.assertEqual(os.path.getmtime(snap_path), first_mtime)

    def test_snapshot_failure_does_not_break_load(self):
        # Snapshots are best-effort: a write failure must not propagate.
        self._seed_drafts(["aaaa1111"])
        with mock.patch("builtins.open", side_effect=PermissionError("nope")):
            # The load itself reads from the JSON store via `open` too, so
            # it'll fall back to an empty list -- but it must NOT raise.
            try:
                amplify_app._load_email_drafts()
            except Exception as e:  # pragma: no cover -- defensive
                self.fail(f"_load_email_drafts raised on snapshot failure: {e}")


# ---------------------------------------------------------------------------
# (f) Snapshot writer refusal rules + poison-pill recovery
# ---------------------------------------------------------------------------


class SnapshotRefusalRulesTests(_JsonFallbackBase):
    """Lock in the rules that prevent a same-day empty snapshot from
    poisoning the only on-disk recovery source."""

    def _today_snap_path(self) -> str:
        from datetime import datetime, timezone
        date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
        return os.path.join(
            self._tmpdir,
            f"{amplify_app._EMAIL_DRAFTS_SNAPSHOT_PREFIX}{date_str}.json",
        )

    def test_does_not_overwrite_nonempty_snapshot_with_empty(self):
        snap_path = self._today_snap_path()
        with open(snap_path, "w") as f:
            json.dump({"drafts": [{"id": "keep0001", "name": "n", "ts": 1.0,
                                   "status": "draft", "last_published_ts": 0,
                                   "last_recipient_count": 0, "snapshot": {}}]},
                      f)
        amplify_app._maybe_write_daily_drafts_snapshot({"drafts": []})
        with open(snap_path, "r") as f:
            payload = json.load(f)
        self.assertEqual(len(payload["drafts"]), 1)
        self.assertEqual(payload["drafts"][0]["id"], "keep0001")

    def test_overwrites_empty_same_day_snapshot_with_real_drafts(self):
        # The exact poison-pill scenario: an earlier load wrote an empty
        # same-day snapshot while the table was wiped. Once real drafts
        # are loaded later in the day, that empty file MUST be replaced.
        snap_path = self._today_snap_path()
        with open(snap_path, "w") as f:
            json.dump({"drafts": []}, f)
        amplify_app._maybe_write_daily_drafts_snapshot({
            "drafts": [{"id": "real0001", "name": "n", "ts": 1.0,
                        "status": "draft", "last_published_ts": 0,
                        "last_recipient_count": 0, "snapshot": {}}]
        })
        with open(snap_path, "r") as f:
            payload = json.load(f)
        self.assertEqual(len(payload["drafts"]), 1)
        self.assertEqual(payload["drafts"][0]["id"], "real0001")

    def test_refuses_first_empty_snapshot_when_older_nonempty_exists(self):
        # Yesterday's snapshot has data. A new (empty) load today must
        # NOT write today's empty snapshot, otherwise retention pruning
        # would eventually delete the only good snapshot and leave only
        # empty ones.
        older_path = os.path.join(
            self._tmpdir,
            f"{amplify_app._EMAIL_DRAFTS_SNAPSHOT_PREFIX}20250101.json",
        )
        with open(older_path, "w") as f:
            json.dump({"drafts": [{"id": "old00001", "name": "n", "ts": 1.0,
                                   "status": "draft", "last_published_ts": 0,
                                   "last_recipient_count": 0, "snapshot": {}}]},
                      f)
        amplify_app._maybe_write_daily_drafts_snapshot({"drafts": []})
        self.assertFalse(
            os.path.exists(self._today_snap_path()),
            "today's empty snapshot was written despite older non-empty file",
        )

    def test_find_latest_nonempty_skips_empty_snapshots(self):
        # Newest file is empty (yesterday's poison pill), older one has
        # data. Recovery must skip the empty file.
        empty_path = os.path.join(
            self._tmpdir,
            f"{amplify_app._EMAIL_DRAFTS_SNAPSHOT_PREFIX}20260430.json",
        )
        good_path = os.path.join(
            self._tmpdir,
            f"{amplify_app._EMAIL_DRAFTS_SNAPSHOT_PREFIX}20260429.json",
        )
        with open(empty_path, "w") as f:
            json.dump({"drafts": []}, f)
        with open(good_path, "w") as f:
            json.dump({"drafts": [{"id": "rec00001", "name": "n", "ts": 1.0,
                                   "status": "draft", "last_published_ts": 0,
                                   "last_recipient_count": 0, "snapshot": {}}]},
                      f)
        fn, drafts = amplify_app._find_latest_nonempty_snapshot()
        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0]["id"], "rec00001")
        self.assertTrue(fn.endswith("20260429.json"))


# ---------------------------------------------------------------------------
# (g) Postgres wipe -> snapshot restore
# ---------------------------------------------------------------------------


class _FakePgCursor:
    def __init__(self, conn):
        self._conn = conn
        self._last_select = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params=None):
        sql_norm = " ".join(sql.split()).upper()
        if sql_norm.startswith("CREATE TABLE") or sql_norm.startswith("ALTER TABLE"):
            return
        if sql_norm.startswith("SELECT COUNT"):
            self._last_select = "count"
            return
        if sql_norm.startswith("SELECT"):
            self._last_select = "rows"
            return
        if sql_norm.startswith("INSERT"):
            # Mimic ON CONFLICT DO NOTHING: insert a row.
            row = (
                params[0], params[1], params[2], params[3],
                params[4], params[5], params[6], params[7], params[8],
            )
            self._conn.rows.append(row)
            return
        # default: ignore

    def fetchone(self):
        if self._last_select == "count":
            return (len(self._conn.rows),)
        return None

    def fetchall(self):
        # Newest first by ts (column index 2)
        return sorted(self._conn.rows, key=lambda r: -float(r[2] or 0))


class _FakePgConn:
    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.committed = 0

    def cursor(self):
        return _FakePgCursor(self)

    def commit(self):
        self.committed += 1

    def rollback(self):
        pass

    def close(self):
        pass


class WipeRecoveryFromSnapshotTests(unittest.TestCase):
    """Postgres returns zero rows, but a non-empty snapshot is on disk:
    `_load_email_drafts` must restore that snapshot back into Postgres
    and return the restored drafts (never a blank My Content)."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="email_drafts_recovery_")
        self._orig_cache_dir = amplify_app._FEATURES_CACHE_DIR
        amplify_app._FEATURES_CACHE_DIR = self._tmpdir
        self._patch_url = mock.patch.object(
            amplify_app, "_DRAFTS_DB_URL", "postgres://test"
        )
        self._patch_url.start()
        # Force `_ensure_drafts_table` to short-circuit so we don't try
        # to issue real DDL.
        self._orig_initialized = amplify_app._drafts_db_initialized
        amplify_app._drafts_db_initialized = True

    def tearDown(self):
        self._patch_url.stop()
        amplify_app._drafts_db_initialized = self._orig_initialized
        amplify_app._FEATURES_CACHE_DIR = self._orig_cache_dir
        try:
            for fn in os.listdir(self._tmpdir):
                try:
                    os.unlink(os.path.join(self._tmpdir, fn))
                except OSError:
                    pass
            os.rmdir(self._tmpdir)
        except OSError:
            pass

    def _write_snapshot(self, fn: str, drafts: list):
        path = os.path.join(self._tmpdir, fn)
        with open(path, "w") as f:
            json.dump({"drafts": drafts}, f)

    def test_empty_db_with_nonempty_snapshot_triggers_restore(self):
        snap_drafts = [{
            "id": "rec00001", "name": "Restored draft", "ts": 1700000000.0,
            "status": "draft", "last_published_ts": 0,
            "last_recipient_count": 0, "snapshot": {"channel": "email_standalone"},
            "category": None, "downloaded_ts": None,
        }]
        self._write_snapshot(
            f"{amplify_app._EMAIL_DRAFTS_SNAPSHOT_PREFIX}20260430.json",
            snap_drafts,
        )
        fake_conn = _FakePgConn(rows=[])
        with mock.patch.object(amplify_app, "_drafts_db_conn", return_value=fake_conn):
            result = amplify_app._load_email_drafts()
        self.assertEqual(len(result["drafts"]), 1)
        self.assertEqual(result["drafts"][0]["id"], "rec00001")
        # The restore MUST have written the row back into Postgres so a
        # subsequent crash / load doesn't lose it again.
        self.assertEqual(len(fake_conn.rows), 1)
        self.assertGreaterEqual(fake_conn.committed, 1)

    def test_empty_db_with_no_snapshot_returns_empty_without_error(self):
        # Brand-new install: empty DB, no on-disk snapshots. Must NOT
        # raise and must return an empty list.
        fake_conn = _FakePgConn(rows=[])
        with mock.patch.object(amplify_app, "_drafts_db_conn", return_value=fake_conn):
            result = amplify_app._load_email_drafts()
        self.assertEqual(result["drafts"], [])

    def test_nonempty_db_does_not_trigger_restore(self):
        # Snapshot has data, but DB also has data: the DB wins (no
        # restore from snapshot -- snapshot is a recovery source only).
        self._write_snapshot(
            f"{amplify_app._EMAIL_DRAFTS_SNAPSHOT_PREFIX}20260430.json",
            [{"id": "snap0001", "name": "n", "ts": 1.0, "status": "draft",
              "last_published_ts": 0, "last_recipient_count": 0,
              "snapshot": {}, "category": None, "downloaded_ts": None}],
        )
        existing_row = (
            "live0001", "Live", 9999999999.0, "draft", 0, 0,
            json.dumps({}), None, None,
        )
        fake_conn = _FakePgConn(rows=[existing_row])
        with mock.patch.object(amplify_app, "_drafts_db_conn", return_value=fake_conn):
            result = amplify_app._load_email_drafts()
        ids = [d["id"] for d in result["drafts"]]
        self.assertEqual(ids, ["live0001"])
        self.assertEqual(len(fake_conn.rows), 1)


if __name__ == "__main__":
    unittest.main()
