"""Tests for the shared attachment storage seam (Task #99).

Covers:
  * ``integrations.attachment_store.put`` — S3 success and failure paths,
    plus the implicit local fallback when the backend is disabled or
    secrets are missing.
  * Per-kind serve-route S3 redirects in ``app.py`` and
    ``announcements_routes.py``.
  * Per-kind admin backfill endpoint happy paths.

No real boto3 / S3 calls happen: the S3 client is monkey-patched to a
fake object that records ``put_object`` calls and (optionally) raises a
``BotoCoreError`` to simulate an S3 outage.

Run with:
    cd artifacts/amplify && python -m unittest tests.test_attachment_store_seam
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import sys
import tempfile
import unittest
from io import BytesIO
from unittest import mock

# Ensure ``artifacts/amplify`` is on sys.path so we can import the app
# package the same way ``run.sh`` does.
_HERE = os.path.dirname(os.path.abspath(__file__))
_AMPLIFY_DIR = os.path.dirname(_HERE)
if _AMPLIFY_DIR not in sys.path:
    sys.path.insert(0, _AMPLIFY_DIR)

# Heavy app import (Flask app + every blueprint). This is fine — none of
# the imports talk to the network at import time.
import app as amp_app  # noqa: E402
import announcements_routes as ann_routes  # noqa: E402
from ai import publish_store  # noqa: E402
from integrations import attachment_store  # noqa: E402
from integrations import video_thumb  # noqa: E402

ADMIN_TOKEN = "test-admin-token"

# A minimal but valid 1x1 PNG (the same bytes we ship as the video
# placeholder) so backfill code that decodes the data URL succeeds.
_PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108020000"
    "00907753de0000000c4944415478da6360606000000000050001a5f6"
    "45400000000049454e44ae426082"
)
_PNG_DATA_URL = "data:image/png;base64," + base64.b64encode(_PNG_BYTES).decode("ascii")


def _fake_s3_client(record: list, *, raise_on_put: bool = False):
    """Build a fake boto3 S3 client.

    ``record`` collects each ``put_object`` call so tests can assert that
    the bytes really moved through the seam. When ``raise_on_put`` is
    True the fake raises a real ``BotoCoreError`` so the seam exercises
    its except branch.
    """
    from botocore.exceptions import BotoCoreError

    class _FakeClient:
        def put_object(self, **kwargs):
            record.append(kwargs)
            if raise_on_put:
                raise BotoCoreError()
            return {"ETag": '"deadbeef"'}

        def delete_object(self, **kwargs):
            record.append({"_op": "delete", **kwargs})
            return {}

        def generate_presigned_url(self, **kwargs):
            return "https://fake.example.com/presigned"

    return _FakeClient()


# ---------------------------------------------------------------------------
# attachment_store.put — pure unit tests
# ---------------------------------------------------------------------------


class AttachmentStorePutTests(unittest.TestCase):
    """Direct tests of the put() seam with no Flask in the picture."""

    def setUp(self):
        # Fixed fake S3 env so we don't depend on what's actually
        # configured in this Repl.
        self._env_patch = mock.patch.dict(
            os.environ,
            {
                "AMPLIFY_IMAGE_STORAGE_BACKEND": "s3",
                "S3_Bucket_name": "test-bucket",
                "S3_Region": "us-east-1",
                "S3_Access_Key": "AKIAFAKEFAKEFAKE",
                "S3_Secret_Access_Key": "fake-secret/+abc",
            },
            clear=False,
        )
        self._env_patch.start()
        # Reset the recent-uploads ring so assertions don't trip on
        # leftovers from another test in this process.
        with attachment_store._RECENT_LOCK:
            attachment_store._RECENT.clear()

    def tearDown(self):
        self._env_patch.stop()

    def test_secrets_present_and_s3_enabled(self):
        self.assertEqual(attachment_store.get_backend_name(), "s3")
        self.assertTrue(attachment_store.s3_enabled())
        self.assertTrue(all(attachment_store.secrets_present().values()))

    def test_put_empty_bytes_returns_local_with_error(self):
        out = attachment_store.put("feature-images", "x.png", b"")
        self.assertEqual(out["backend"], "local")
        self.assertIsNone(out["url"])
        self.assertIsNone(out["key"])
        self.assertEqual(out["error"], "empty_bytes")
        self.assertEqual(out["bytes"], 0)

    def test_put_local_backend_returns_local(self):
        with mock.patch.dict(os.environ, {"AMPLIFY_IMAGE_STORAGE_BACKEND": "local"}):
            out = attachment_store.put("feature-images", "x.png", b"abc")
        self.assertEqual(out["backend"], "local")
        self.assertIsNone(out["url"])
        self.assertIsNone(out["key"])
        self.assertIsNone(out["error"])

    def test_put_missing_secret_returns_local_with_error(self):
        with mock.patch.dict(os.environ, {"S3_Bucket_name": ""}):
            out = attachment_store.put("feature-images", "x.png", b"abc")
        self.assertEqual(out["backend"], "local")
        self.assertIsNone(out["key"])
        self.assertIn("missing secrets", (out["error"] or ""))

    def test_put_s3_success_uploads_and_returns_key_and_url(self):
        record: list = []
        with mock.patch.object(
            attachment_store, "_s3_client", return_value=_fake_s3_client(record)
        ):
            out = attachment_store.put(
                kind="feature-images",
                key_hint="abc123.png",
                raw_bytes=_PNG_BYTES,
                content_type="image/png",
            )
        self.assertEqual(out["backend"], "s3")
        self.assertIsNone(out["error"])
        self.assertTrue(out["key"].startswith("feature-images/"))
        self.assertTrue(out["key"].endswith(".png"))
        self.assertEqual(
            out["url"], f"https://test-bucket.s3.us-east-1.amazonaws.com/{out['key']}"
        )
        self.assertEqual(len(record), 1)
        call = record[0]
        self.assertEqual(call["Bucket"], "test-bucket")
        self.assertEqual(call["Key"], out["key"])
        self.assertEqual(call["Body"], _PNG_BYTES)
        self.assertEqual(call["ContentType"], "image/png")
        # Recent ring buffer recorded the success.
        recent = attachment_store.recent_uploads(limit=5)
        self.assertEqual(len(recent), 1)
        self.assertTrue(recent[0]["ok"])
        self.assertEqual(recent[0]["kind"], "feature-images")

    def test_put_s3_failure_returns_local_fallback(self):
        record: list = []
        with mock.patch.object(
            attachment_store,
            "_s3_client",
            return_value=_fake_s3_client(record, raise_on_put=True),
        ):
            out = attachment_store.put(
                kind="videos",
                key_hint="some-video.mp4",
                raw_bytes=b"\x00\x01\x02fake-mp4",
                content_type="video/mp4",
            )
        # Caller MUST treat this as "use the local disk path".
        self.assertEqual(out["backend"], "local")
        self.assertIsNone(out["url"])
        self.assertIsNotNone(out["error"])
        self.assertEqual(out["kind"], "videos")
        # We recorded the attempt with ok=False.
        recent = attachment_store.recent_uploads(limit=5)
        self.assertEqual(len(recent), 1)
        self.assertFalse(recent[0]["ok"])

    def test_put_kind_with_explicit_path_hint_keeps_path(self):
        """When the hint is already a full ``kind/...`` path we keep it
        verbatim (used for video thumbs and external thumbs that need a
        deterministic key)."""
        record: list = []
        with mock.patch.object(
            attachment_store, "_s3_client", return_value=_fake_s3_client(record)
        ):
            out = attachment_store.put(
                kind="video-thumbs",
                key_hint="videos/vid-123/thumb.jpg",
                raw_bytes=b"jpegbytes",
                content_type="image/jpeg",
            )
        # video-thumbs share the "videos" prefix so the deterministic
        # ``videos/<id>/thumb.jpg`` key passes through untouched.
        self.assertEqual(out["backend"], "s3")
        self.assertEqual(out["key"], "videos/vid-123/thumb.jpg")


# ---------------------------------------------------------------------------
# Per-kind serve-route S3 redirect tests
# ---------------------------------------------------------------------------


class ServeRouteRedirectTests(unittest.TestCase):
    """Each per-kind serve route should 302-redirect to S3 when a key was
    recorded. We patch the data lookups so no DB / disk is needed."""

    def setUp(self):
        amp_app.app.config["TESTING"] = True
        self.client = amp_app.app.test_client()

        self._env_patch = mock.patch.dict(
            os.environ,
            {
                "AMPLIFY_IMAGE_STORAGE_BACKEND": "s3",
                "S3_Bucket_name": "test-bucket",
                "S3_Region": "us-east-1",
                "S3_Access_Key": "AKIAFAKE",
                "S3_Secret_Access_Key": "fake-secret",
            },
            clear=False,
        )
        self._env_patch.start()

    def tearDown(self):
        self._env_patch.stop()

    # --- feature-images ----------------------------------------------------

    def test_serve_feature_image_redirects_to_s3(self):
        s3_url = "https://test-bucket.s3.us-east-1.amazonaws.com/feature-images/abc.png"
        with mock.patch.object(
            amp_app, "get_publish_image",
            return_value={
                "s3_url": s3_url,
                "s3_key": "feature-images/abc.png",
                "name": "abc.png",
            },
        ):
            r = self.client.get("/api/publish/image/serve/feat-1")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers["Location"], s3_url)

    def test_serve_feature_image_falls_back_to_data_url_without_s3(self):
        with mock.patch.object(
            amp_app, "get_publish_image",
            return_value={
                "s3_url": "",
                "s3_key": "",
                "dataUrl": _PNG_DATA_URL,
                "name": "abc.png",
            },
        ):
            r = self.client.get("/api/publish/image/serve/feat-2")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, "image/png")

    # --- hosted-emails -----------------------------------------------------

    def test_serve_hosted_image_redirects_to_s3(self):
        # img_id must match the [a-f0-9]+ sanitiser.
        img_id = "abc123def4567890"
        s3_url = (
            "https://test-bucket.s3.us-east-1.amazonaws.com/hosted-emails/" + img_id + ".png"
        )
        with mock.patch.object(
            amp_app,
            "_load_hosted_image_s3_meta",
            return_value=("hosted-emails/" + img_id + ".png", s3_url, "png"),
        ):
            r = self.client.get(f"/api/publish/image/hosted/{img_id}")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers["Location"], s3_url)

    # --- videos ------------------------------------------------------------

    def test_serve_video_redirects_to_s3(self):
        s3_url = "https://test-bucket.s3.us-east-1.amazonaws.com/videos/abc/video.mp4"
        with mock.patch.object(
            publish_store, "get_video_meta",
            return_value={
                "s3_url": s3_url,
                "s3_key": "videos/abc/video.mp4",
                "ext": ".mp4",
            },
        ):
            r = self.client.get("/api/videos/abc")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers["Location"], s3_url)

    # --- video-thumbs ------------------------------------------------------

    def test_serve_video_thumb_redirects_to_s3(self):
        s3_url = "https://test-bucket.s3.us-east-1.amazonaws.com/videos/abc/thumb.jpg"
        with mock.patch.object(
            publish_store, "get_video_meta",
            return_value={
                "s3_thumb_url": s3_url,
                "s3_thumb_key": "videos/abc/thumb.jpg",
                "ext": ".mp4",
            },
        ):
            r = self.client.get("/api/videos/abc/thumb")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers["Location"], s3_url)

    # --- external-thumbs ---------------------------------------------------

    def test_serve_external_thumb_redirects_to_s3(self):
        # Key must match [a-f0-9]{1,64}.
        key = "deadbeef"
        s3_url = (
            "https://test-bucket.s3.us-east-1.amazonaws.com/external-thumbs/"
            + key
            + ".jpg"
        )
        with mock.patch.object(video_thumb, "get_external_thumb_s3_url", return_value=s3_url):
            r = self.client.get(f"/api/videos/external-thumb/{key}")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers["Location"], s3_url)

    # --- announcements -----------------------------------------------------

    def test_serve_announcement_upload_redirects_via_sidecar(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(ann_routes, "UPLOAD_DIR", tmp):
                stored_name = "1700000000_token_demo.png"
                with open(os.path.join(tmp, stored_name), "wb") as f:
                    f.write(_PNG_BYTES)
                s3_url = (
                    "https://test-bucket.s3.us-east-1.amazonaws.com/announcements/"
                    + stored_name
                )
                with open(os.path.join(tmp, stored_name + ".s3"), "w") as f:
                    f.write(f"announcements/{stored_name}\n{s3_url}\n")
                r = self.client.get(f"/api/admin/announcement-uploads/{stored_name}")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers["Location"], s3_url)


# ---------------------------------------------------------------------------
# Backfill endpoint per-kind happy paths
# ---------------------------------------------------------------------------


class BackfillEndpointTests(unittest.TestCase):
    """Drive the admin backfill endpoint per kind, asserting that the
    fake S3 client recorded an upload and that local sidecar / metadata
    files were updated to point at the new key."""

    def setUp(self):
        amp_app.app.config["TESTING"] = True
        self.client = amp_app.app.test_client()

        # Per-test scratch dirs, each redirected onto the corresponding
        # module-level constant so the backfill scanners see only our
        # fixtures.
        self.tmp_root = tempfile.mkdtemp(prefix="att-tests-")
        self.images_dir = os.path.join(self.tmp_root, "images")
        self.videos_dir = os.path.join(self.tmp_root, "videos")
        self.uploads_dir = os.path.join(self.tmp_root, "uploads")
        self.ext_thumbs_dir = os.path.join(self.tmp_root, "ext_thumbs")
        for d in (self.images_dir, self.videos_dir, self.uploads_dir, self.ext_thumbs_dir):
            os.makedirs(d, exist_ok=True)

        self._patches = [
            mock.patch.object(publish_store, "IMAGES_DIR", self.images_dir),
            mock.patch.object(publish_store, "VIDEOS_DIR", self.videos_dir),
            mock.patch.object(ann_routes, "UPLOAD_DIR", self.uploads_dir),
            mock.patch.object(video_thumb, "_CACHE_DIR", self.ext_thumbs_dir),
            mock.patch.dict(
                os.environ,
                {
                    "AMPLIFY_ADMIN_TOKEN": ADMIN_TOKEN,
                    "AMPLIFY_IMAGE_STORAGE_BACKEND": "s3",
                    "S3_Bucket_name": "test-bucket",
                    "S3_Region": "us-east-1",
                    "S3_Access_Key": "AKIAFAKE",
                    "S3_Secret_Access_Key": "fake-secret",
                },
                clear=False,
            ),
        ]
        for p in self._patches:
            p.start()

        self.s3_calls: list = []
        self._client_patch = mock.patch.object(
            attachment_store, "_s3_client", return_value=_fake_s3_client(self.s3_calls)
        )
        self._client_patch.start()

    def tearDown(self):
        self._client_patch.stop()
        for p in reversed(self._patches):
            p.stop()
        shutil.rmtree(self.tmp_root, ignore_errors=True)

    def _call_backfill(self, kind: str, limit: int = 50):
        return self.client.post(
            "/api/admin/attachments/backfill",
            json={"kind": kind, "limit": limit, "admin_token": ADMIN_TOKEN},
        )

    # --- feature-images ----------------------------------------------------

    def test_backfill_feature_images_uploads_and_records_s3_key(self):
        feature_id = "feat-xyz"
        with open(os.path.join(self.images_dir, f"{feature_id}.img"), "w") as f:
            f.write(_PNG_DATA_URL)
        with open(os.path.join(self.images_dir, f"{feature_id}.meta.json"), "w") as f:
            json.dump({"name": "demo.png", "size": len(_PNG_BYTES), "is_gif": False}, f)

        r = self._call_backfill("feature-images")
        self.assertEqual(r.status_code, 200, r.data)
        body = r.get_json()
        self.assertTrue(body["success"])
        self.assertEqual(body["uploaded"], 1)
        self.assertEqual(body["errors"], 0)

        with open(os.path.join(self.images_dir, f"{feature_id}.meta.json")) as f:
            meta = json.load(f)
        self.assertTrue(meta["s3_key"].startswith("feature-images/"))
        self.assertTrue(meta["s3_url"].startswith("https://test-bucket.s3."))
        self.assertEqual(len(self.s3_calls), 1)

    # --- videos & video-thumbs --------------------------------------------

    def _seed_video(self, video_id: str = "vid-1") -> str:
        vdir = os.path.join(self.videos_dir, video_id)
        os.makedirs(vdir, exist_ok=True)
        with open(os.path.join(vdir, "video.mp4"), "wb") as f:
            f.write(b"\x00\x01\x02fake-mp4-bytes")
        with open(os.path.join(vdir, "thumb.jpg"), "wb") as f:
            f.write(b"\xff\xd8\xff\xe0fake-jpeg")
        with open(os.path.join(vdir, "meta.json"), "w") as f:
            json.dump(
                {"feature_id": "feat-xyz", "ext": ".mp4", "filename": "v.mp4"}, f
            )
        return vdir

    def test_backfill_videos_uploads_and_records_s3_key(self):
        vdir = self._seed_video("vid-1")
        # ``_db_upsert_video`` is best-effort and tries to talk to
        # Postgres; stub it so the test stays self-contained.
        with mock.patch.object(publish_store, "_db_upsert_video", return_value=None):
            r = self._call_backfill("videos")
        self.assertEqual(r.status_code, 200, r.data)
        body = r.get_json()
        self.assertTrue(body["success"])
        self.assertGreaterEqual(body["uploaded"], 1)

        with open(os.path.join(vdir, "meta.json")) as f:
            meta = json.load(f)
        self.assertTrue(meta["s3_key"].startswith("videos/"))
        # Without thumbs requested, thumb keys stay empty.
        self.assertNotIn("s3_thumb_key", meta)
        # The video bytes did flow through the seam.
        kinds_uploaded = {c["Key"].split("/", 1)[0] for c in self.s3_calls}
        self.assertIn("videos", kinds_uploaded)

    def test_backfill_video_thumbs_uploads_and_records_thumb_key(self):
        vdir = self._seed_video("vid-2")
        with mock.patch.object(publish_store, "_db_upsert_video", return_value=None):
            r = self._call_backfill("video-thumbs")
        self.assertEqual(r.status_code, 200, r.data)
        body = r.get_json()
        self.assertTrue(body["success"])
        self.assertGreaterEqual(body["thumbs_uploaded"], 1)

        with open(os.path.join(vdir, "meta.json")) as f:
            meta = json.load(f)
        self.assertTrue(meta.get("s3_thumb_key", "").startswith("videos/"))

    # --- external-thumbs ---------------------------------------------------

    def test_backfill_external_thumbs_uploads_jpegs(self):
        # The cache stores ``<key>.jpg`` files; key uses [a-f0-9].
        with open(os.path.join(self.ext_thumbs_dir, "deadbeef.jpg"), "wb") as f:
            f.write(b"\xff\xd8\xff\xe0fake-jpeg")
        r = self._call_backfill("external-thumbs")
        self.assertEqual(r.status_code, 200, r.data)
        body = r.get_json()
        self.assertTrue(body["success"])
        self.assertEqual(body["uploaded"], 1)
        self.assertEqual(body["errors"], 0)
        # The seam saw an external-thumbs put.
        keys = [c["Key"] for c in self.s3_calls]
        self.assertTrue(any(k.startswith("external-thumbs/") for k in keys))

    # --- hosted-emails (disk-fallback path; DB path needs Postgres) -------

    def test_backfill_hosted_emails_uploads_disk_fallback(self):
        img_id = "abc123def4567890"
        d = os.path.join(self.images_dir, "_hosted_" + img_id)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "image.dat"), "w") as f:
            f.write(_PNG_DATA_URL)
        with open(os.path.join(d, "meta.json"), "w") as f:
            json.dump({"id": img_id, "ext": "png", "name": "img.png"}, f)

        # Force the DB-backed first half of the backfiller to skip — we
        # don't have a Postgres in this test process.
        with mock.patch.object(amp_app, "_drafts_db_conn", return_value=None), \
                mock.patch.object(amp_app, "_set_hosted_image_s3", return_value=True):
            r = self._call_backfill("hosted-emails")
        self.assertEqual(r.status_code, 200, r.data)
        body = r.get_json()
        self.assertTrue(body["success"])
        self.assertEqual(body["uploaded"], 1)
        self.assertEqual(body["errors"], 0)

        # Sidecar marker should be written so we don't re-upload.
        marker = os.path.join(d, ".s3")
        self.assertTrue(os.path.isfile(marker))
        with open(marker) as f:
            marker_text = f.read()
        self.assertIn("hosted-emails/", marker_text)
        keys = [c["Key"] for c in self.s3_calls]
        self.assertTrue(any(k.startswith("hosted-emails/") for k in keys))

    # --- announcements -----------------------------------------------------

    def test_backfill_announcements_uploads_and_writes_sidecar(self):
        stored_name = "1700000000_token_demo.png"
        full = os.path.join(self.uploads_dir, stored_name)
        with open(full, "wb") as f:
            f.write(_PNG_BYTES)

        r = self._call_backfill("announcements")
        self.assertEqual(r.status_code, 200, r.data)
        body = r.get_json()
        self.assertTrue(body["success"])
        self.assertEqual(body["uploaded"], 1)
        self.assertEqual(body["errors"], 0)

        sidecar = full + ".s3"
        self.assertTrue(os.path.isfile(sidecar))
        with open(sidecar) as f:
            sidecar_text = f.read()
        self.assertIn("announcements/", sidecar_text)

    # --- gating ------------------------------------------------------------

    def test_backfill_requires_admin_token(self):
        r = self.client.post("/api/admin/attachments/backfill", json={"kind": "videos"})
        self.assertEqual(r.status_code, 401)

    def test_backfill_returns_503_when_s3_disabled(self):
        with mock.patch.dict(os.environ, {"AMPLIFY_IMAGE_STORAGE_BACKEND": "local"}):
            r = self._call_backfill("videos")
        self.assertEqual(r.status_code, 503)
        body = r.get_json()
        self.assertFalse(body["success"])
        self.assertEqual(body["error"], "s3_disabled")

    def test_backfill_unknown_kind_returns_400(self):
        r = self._call_backfill("not-a-kind")
        self.assertEqual(r.status_code, 400)
        body = r.get_json()
        self.assertEqual(body["error"], "unknown_kind")


# ---------------------------------------------------------------------------
# Admin status endpoint smoke-test
# ---------------------------------------------------------------------------


class AttachmentStatusEndpointTests(unittest.TestCase):
    def setUp(self):
        amp_app.app.config["TESTING"] = True
        self.client = amp_app.app.test_client()
        self._env = mock.patch.dict(
            os.environ,
            {
                "AMPLIFY_ADMIN_TOKEN": ADMIN_TOKEN,
                "AMPLIFY_IMAGE_STORAGE_BACKEND": "s3",
                "S3_Bucket_name": "test-bucket",
                "S3_Region": "us-east-1",
                "S3_Access_Key": "AKIAFAKE",
                "S3_Secret_Access_Key": "fake-secret",
            },
            clear=False,
        )
        self._env.start()

    def tearDown(self):
        self._env.stop()

    def test_status_reports_backend_and_secrets(self):
        # Make pending-counts return a stable shape so we don't have to
        # stand up Postgres.
        with mock.patch.object(
            amp_app,
            "_attachments_pending_counts",
            return_value={
                "feature-images": 0,
                "videos": 0,
                "video-thumbs": 0,
                "external-thumbs": 0,
                "hosted-emails": 0,
                "announcements": 0,
            },
        ):
            r = self.client.get(
                "/api/admin/attachments/status",
                headers={"X-Admin-Token": ADMIN_TOKEN},
            )
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body["success"])
        self.assertEqual(body["backend"], "s3")
        self.assertTrue(body["s3_enabled"])
        self.assertTrue(all(body["secrets_present"].values()))
        self.assertIn("pending", body)
        self.assertIn("recent", body)


if __name__ == "__main__":
    unittest.main()
