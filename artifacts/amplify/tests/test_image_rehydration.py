"""Tests for the image-rehydration safety net used after a hard refresh.

Background
----------
When a marketer uploads a screenshot inside the combined-email composer,
two things happen client-side:
  1. The image dataUrl is stashed in `window.prepBatchImages[batchIdx]`
     (in-memory, lost on refresh) and the body gets a marker like
     ``[image: shot.png]``.
  2. The same image is POSTed to ``/api/publish/image`` keyed by
     ``feature_id`` so it is durable on the server.

When the user refreshes the page, the snapshot stored in
``email_drafts`` may NOT carry the dataUrl (the snapshot save can fail
silently when oversized, or the upload may have happened after the
last save). The frontend then has stale ``[image: shot.png]`` markers
in the body but no binary, so the live preview shows broken images.

The fix: after `loadEmailDraft` restores the snapshot, the frontend
fetches ``/api/publish/image/meta/<fid>`` for every feature missing an
image entry and rehydrates ``prepBatchImages`` from the durable copy.

These tests lock in the contracts that fallback path depends on:
  * `/api/publish/image/meta/<fid>` returns ``{exists, name, dataUrl,
    isGif}`` after the upload endpoint stored the image.
  * It returns 404 with ``{exists: False}`` when no image exists for
    the feature, so the frontend's per-feature fetch fails gracefully
    instead of poisoning the preview with a placeholder.
  * `save_email_draft` returns a JSON ``error`` field with HTTP 413
    when the snapshot exceeds the size cap, so the publish flow can
    surface a "snapshot save failed" toast instead of swallowing it.

Run with:
    cd artifacts/amplify && python -m unittest tests.test_image_rehydration
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


_TINY_PNG_DATA_URL = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)


class PublishImageMetaContractTests(unittest.TestCase):
    """The frontend rehydration relies on the meta endpoint's exact shape."""

    def setUp(self):
        self.client = amplify_app.app.test_client()

    def test_meta_returns_dataurl_after_upload(self):
        # Stub the publish_store so we don't write to a real backing store
        # but exercise the real Flask handlers end to end.
        stored = {}

        def _save(feature_id, channel, dataurl_or_url, name, size, is_gif=False):
            stored[feature_id] = {
                "dataUrl": dataurl_or_url,
                "name": name,
                "is_gif": is_gif,
            }

        def _get(feature_id):
            return stored.get(feature_id)

        with mock.patch.object(amplify_app, "save_publish_image", side_effect=_save), \
             mock.patch.object(amplify_app, "get_publish_image", side_effect=_get):
            rv = self.client.post(
                "/api/publish/image",
                data=json.dumps({
                    "feature_id": "feat-rehydrate",
                    "dataUrl": _TINY_PNG_DATA_URL,
                    "name": "shot.png",
                    "size": 68,
                }),
                content_type="application/json",
            )
            self.assertEqual(rv.status_code, 200)

            rv = self.client.get("/api/publish/image/meta/feat-rehydrate")
            self.assertEqual(rv.status_code, 200)
            body = json.loads(rv.data)
            # The four fields the rehydrator reads. Locking the keys
            # here means a future rename of the endpoint contract will
            # fail this test instead of silently breaking the refresh
            # path in production.
            self.assertTrue(body.get("exists"))
            self.assertEqual(body.get("name"), "shot.png")
            self.assertEqual(body.get("dataUrl"), _TINY_PNG_DATA_URL)
            self.assertFalse(body.get("isGif"))

    def test_meta_returns_404_when_missing(self):
        # The rehydrator treats 404 as "nothing to restore" and silently
        # skips the feature — important so a refresh on an artifact with
        # no inline images doesn't spam errors.
        with mock.patch.object(amplify_app, "get_publish_image", return_value=None):
            rv = self.client.get("/api/publish/image/meta/feat-missing")
            self.assertEqual(rv.status_code, 404)
            body = json.loads(rv.data)
            self.assertFalse(body.get("exists"))


class OversizedSnapshotErrorContractTests(unittest.TestCase):
    """publishCombinedEmail's toast wiring expects a JSON `error` body on 413."""

    def setUp(self):
        # JSON fallback so this is hermetic.
        self._patch_conn = mock.patch.object(amplify_app, "_drafts_db_conn", return_value=None)
        self._patch_conn.start()
        self._tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self._tmp.write(b'{"drafts":[]}')
        self._tmp.close()
        self._orig_path = amplify_app._EMAIL_DRAFTS_PATH
        amplify_app._EMAIL_DRAFTS_PATH = self._tmp.name
        self.client = amplify_app.app.test_client()

    def tearDown(self):
        self._patch_conn.stop()
        amplify_app._EMAIL_DRAFTS_PATH = self._orig_path
        try:
            os.unlink(self._tmp.name)
        except OSError:
            pass

    def test_oversized_snapshot_returns_413_with_error_field(self):
        # Construct a snapshot just over the per-draft byte cap so
        # `save_email_draft` rejects it. We stuff one giant fake dataUrl
        # into imagesByFeatureId because that is the realistic shape
        # users hit when they attach a high-resolution screenshot.
        big_blob = "A" * (amplify_app._EMAIL_DRAFT_MAX_BYTES + 1024)
        snapshot = {
            "channel": "email_standalone",
            "results": [{"feature_id": "feat-1", "feature_title": "x"}],
            "imagesByFeatureId": {"feat-1": {"name": "huge.png", "dataUrl": big_blob, "size": len(big_blob)}},
            "combined": {"subject": "s", "body": "[image: huge.png]"},
        }
        rv = self.client.post(
            "/api/email-drafts",
            data=json.dumps({"id": "oversize1", "name": "Big draft", "snapshot": snapshot}),
            content_type="application/json",
        )
        self.assertEqual(rv.status_code, 413)
        body = json.loads(rv.data)
        # The frontend reads `error` from this response to drive the
        # "Email sent, but saving the artifact failed" warning toast.
        self.assertIn("error", body)
        self.assertIn("Draft too large", body["error"])


if __name__ == "__main__":
    unittest.main()
