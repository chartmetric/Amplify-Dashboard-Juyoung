"""Tests for the My Content > Downloaded tab plumbing.

Covers:
  * `_draft_summary` exposes `downloaded_ts` (defaulting to None).
  * `save_email_draft` carries forward an existing `downloaded_ts` so editing
    a draft does not erase its presence in the Downloaded tab.
  * `POST /api/email-drafts/<id>/mark-downloaded` stamps the timestamp end
    to end and returns the canonical summary.
  * The JSON-fallback path (no Postgres) does NOT deadlock when
    mark-downloaded acquires the per-store lock and `_save_email_drafts`
    re-acquires it for the disk write. (Regression guard for the
    non-reentrant-lock bug found during code review.)
  * 404 when stamping an unknown id.

Run with:
    cd artifacts/amplify && python -m unittest tests.test_downloaded_tab
"""
from __future__ import annotations

import json
import os
import sys
import time
import threading
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_AMPLIFY_DIR = os.path.dirname(_HERE)
if _AMPLIFY_DIR not in sys.path:
    sys.path.insert(0, _AMPLIFY_DIR)

import app as amplify_app  # noqa: E402


class DraftSummaryDownloadedTsTests(unittest.TestCase):
    def test_summary_exposes_downloaded_ts(self):
        s = amplify_app._draft_summary({"id": "abc", "name": "x", "downloaded_ts": 1234.5})
        self.assertEqual(s.get("downloaded_ts"), 1234.5)

    def test_summary_defaults_downloaded_ts_to_none(self):
        s = amplify_app._draft_summary({"id": "abc", "name": "x"})
        self.assertIsNone(s.get("downloaded_ts"))


class SaveDraftCarriesDownloadedTsTests(unittest.TestCase):
    """Re-saving a draft must not drop its previously-set download stamp."""

    def setUp(self):
        # Force JSON fallback so we don't depend on Postgres being available.
        # Both _DRAFTS_DB_URL and _drafts_db_conn must be neutralized: the
        # loader now distinguishes "configured but blipping" (-> 503) from
        # "not configured" (-> JSON), and that decision is gated on
        # _DRAFTS_DB_URL.
        self._patch_url = mock.patch.object(amplify_app, "_DRAFTS_DB_URL", "")
        self._patch_url.start()
        self._patch_conn = mock.patch.object(amplify_app, "_drafts_db_conn", return_value=None)
        self._patch_conn.start()
        # Isolate the JSON store to a temp file per test.
        import tempfile
        self._tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self._tmp.write(b'{"drafts":[]}')
        self._tmp.close()
        self._orig_path = amplify_app._EMAIL_DRAFTS_PATH
        amplify_app._EMAIL_DRAFTS_PATH = self._tmp.name
        self.client = amplify_app.app.test_client()

    def tearDown(self):
        self._patch_conn.stop()
        self._patch_url.stop()
        amplify_app._EMAIL_DRAFTS_PATH = self._orig_path
        try:
            os.unlink(self._tmp.name)
        except OSError:
            pass

    def _save(self, draft_id, name="Demo", subject="s"):
        return self.client.post(
            "/api/email-drafts",
            data=json.dumps({
                "id": draft_id,
                "name": name,
                "snapshot": {"channel": "email_standalone", "featureIds": ["f1"], "combined": {"subject": subject}},
            }),
            content_type="application/json",
        )

    def test_resaving_preserves_downloaded_ts(self):
        rv = self._save("draftAAA1", name="First save")
        self.assertEqual(rv.status_code, 200)
        rv = self.client.post("/api/email-drafts/draftAAA1/mark-downloaded")
        self.assertEqual(rv.status_code, 200)
        stamped_ts = json.loads(rv.data)["summary"]["downloaded_ts"]
        self.assertIsNotNone(stamped_ts)
        # Now re-save the draft (e.g. user edited the body). The stamp
        # must survive.
        rv = self._save("draftAAA1", name="Second save", subject="edited")
        self.assertEqual(rv.status_code, 200)
        self.assertEqual(json.loads(rv.data)["summary"]["downloaded_ts"], stamped_ts)
        # Confirm via the list endpoint too.
        rv = self.client.get("/api/email-drafts")
        rows = {d["id"]: d for d in json.loads(rv.data)["drafts"]}
        self.assertEqual(rows["draftAAA1"]["downloaded_ts"], stamped_ts)


class MarkDownloadedRouteJsonFallbackTests(unittest.TestCase):
    """End-to-end coverage of the route in JSON-fallback mode.

    Includes a deadlock regression guard: with the lock now an RLock,
    holding it across `_load_email_drafts` + `_save_email_drafts` (which
    re-acquires for the JSON write) must complete promptly.
    """

    def setUp(self):
        # Force JSON fallback (see SaveDraftCarriesDownloadedTsTests for why
        # both _DRAFTS_DB_URL and _drafts_db_conn must be neutralized).
        self._patch_url = mock.patch.object(amplify_app, "_DRAFTS_DB_URL", "")
        self._patch_url.start()
        self._patch_conn = mock.patch.object(amplify_app, "_drafts_db_conn", return_value=None)
        self._patch_conn.start()
        import tempfile
        self._tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self._tmp.write(b'{"drafts":[]}')
        self._tmp.close()
        self._orig_path = amplify_app._EMAIL_DRAFTS_PATH
        amplify_app._EMAIL_DRAFTS_PATH = self._tmp.name
        self.client = amplify_app.app.test_client()
        # Seed one draft so the route has something to stamp.
        self.client.post(
            "/api/email-drafts",
            data=json.dumps({
                "id": "rowZZZ001",
                "name": "Seed",
                "snapshot": {"channel": "email_standalone", "featureIds": ["f1"], "combined": {"subject": "s"}},
            }),
            content_type="application/json",
        )

    def tearDown(self):
        self._patch_conn.stop()
        self._patch_url.stop()
        amplify_app._EMAIL_DRAFTS_PATH = self._orig_path
        try:
            os.unlink(self._tmp.name)
        except OSError:
            pass

    def test_mark_downloaded_stamps_timestamp(self):
        before = time.time()
        rv = self.client.post("/api/email-drafts/rowZZZ001/mark-downloaded")
        after = time.time()
        self.assertEqual(rv.status_code, 200)
        body = json.loads(rv.data)
        self.assertTrue(body.get("success"))
        ts = body["summary"]["downloaded_ts"]
        self.assertIsNotNone(ts)
        self.assertGreaterEqual(ts, before)
        self.assertLessEqual(ts, after)

    def test_mark_downloaded_unknown_id_404s(self):
        rv = self.client.post("/api/email-drafts/missing999/mark-downloaded")
        self.assertEqual(rv.status_code, 404)

    def test_mark_downloaded_does_not_deadlock_in_json_fallback(self):
        """Regression guard for the non-reentrant-lock bug.

        The route holds `_email_drafts_lock` across the load + the call to
        `_save_email_drafts`, which itself acquires the same lock for the
        JSON disk write. With a plain `threading.Lock` this would deadlock
        forever; with an `RLock` (the fix), it returns promptly.
        """
        result = {}

        def _call():
            try:
                rv = self.client.post("/api/email-drafts/rowZZZ001/mark-downloaded")
                result["status"] = rv.status_code
                result["body"] = rv.data
            except Exception as exc:  # pragma: no cover -- defensive
                result["error"] = str(exc)

        t = threading.Thread(target=_call, daemon=True)
        t.start()
        t.join(timeout=5.0)
        self.assertFalse(t.is_alive(), "mark-downloaded JSON-fallback path deadlocked (>5s)")
        self.assertEqual(result.get("status"), 200, result)


if __name__ == "__main__":
    unittest.main()
