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
import time
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

    # All serve routes funnel through ``attachment_store.s3_serve_url``
    # to convert a stored S3 key into the URL the recipient gets
    # 302-redirected to. The current implementation mints a presigned
    # URL (private bucket); a future swap to public-read URLs would
    # change only that helper. Tests mock the helper to a fixed string
    # so they assert the contract — "serve route 302s to whatever
    # s3_serve_url returns" — independent of the URL form.

    # --- feature-images ----------------------------------------------------

    def test_serve_feature_image_redirects_to_s3(self):
        served = "https://signed.example/feature-images/abc.png?sig=token"
        with mock.patch.object(
            amp_app, "get_publish_image",
            return_value={
                "s3_url": "",
                "s3_key": "feature-images/abc.png",
                "name": "abc.png",
            },
        ), mock.patch.object(attachment_store, "s3_serve_url", return_value=served):
            r = self.client.get("/api/publish/image/serve/feat-1")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers["Location"], served)

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
        served = "https://signed.example/hosted-emails/" + img_id + ".png?sig=token"
        with mock.patch.object(
            amp_app,
            "_load_hosted_image_s3_meta",
            return_value=("hosted-emails/" + img_id + ".png", "", "png"),
        ), mock.patch.object(attachment_store, "s3_serve_url", return_value=served):
            r = self.client.get(f"/api/publish/image/hosted/{img_id}")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers["Location"], served)

    # --- videos ------------------------------------------------------------

    def test_serve_video_redirects_to_s3(self):
        served = "https://signed.example/videos/abc/video.mp4?sig=token"
        with mock.patch.object(
            publish_store, "get_video_meta",
            return_value={
                "s3_url": "",
                "s3_key": "videos/abc/video.mp4",
                "ext": ".mp4",
            },
        ), mock.patch.object(attachment_store, "s3_serve_url", return_value=served):
            r = self.client.get("/api/videos/abc")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers["Location"], served)

    # --- video-thumbs ------------------------------------------------------

    def test_serve_video_thumb_redirects_to_s3(self):
        served = "https://signed.example/videos/abc/thumb.jpg?sig=token"
        with mock.patch.object(
            publish_store, "get_video_meta",
            return_value={
                "s3_thumb_url": "",
                "s3_thumb_key": "videos/abc/thumb.jpg",
                "ext": ".mp4",
            },
        ), mock.patch.object(attachment_store, "s3_serve_url", return_value=served):
            r = self.client.get("/api/videos/abc/thumb")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers["Location"], served)

    # --- external-thumbs ---------------------------------------------------

    def test_serve_external_thumb_redirects_to_s3(self):
        # Key must match [a-f0-9]{1,64}.
        key = "deadbeef"
        s3_key = "external-thumbs/" + key + ".jpg"
        served = "https://signed.example/" + s3_key + "?sig=token"
        with mock.patch.object(video_thumb, "get_external_thumb_s3_key", return_value=s3_key), \
             mock.patch.object(attachment_store, "s3_serve_url", return_value=served):
            r = self.client.get(f"/api/videos/external-thumb/{key}")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers["Location"], served)

    # --- announcements -----------------------------------------------------

    def test_serve_announcement_upload_redirects_via_sidecar(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(ann_routes, "UPLOAD_DIR", tmp):
                stored_name = "1700000000_token_demo.png"
                with open(os.path.join(tmp, stored_name), "wb") as f:
                    f.write(_PNG_BYTES)
                s3_key = "announcements/" + stored_name
                served = "https://signed.example/" + s3_key + "?sig=token"
                with open(os.path.join(tmp, stored_name + ".s3"), "w") as f:
                    # The sidecar still records the public URL form for
                    # historical/admin use; the serve route ignores it
                    # and asks s3_serve_url for a working URL instead.
                    public_form = (
                        "https://test-bucket.s3.us-east-1.amazonaws.com/" + s3_key
                    )
                    f.write(f"{s3_key}\n{public_form}\n")
                with mock.patch.object(attachment_store, "s3_serve_url", return_value=served):
                    r = self.client.get(f"/api/admin/announcement-uploads/{stored_name}")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers["Location"], served)


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
        # Task #115: status surfaces whether the long-lived S3 public URL
        # form (the one the email-HTML rewrite embeds into downloaded
        # .html files) is mintable. With bucket+region both set this is
        # always True.
        self.assertTrue(body["direct_s3_url_usable"])


# ---------------------------------------------------------------------------
# Direct-S3 rewrite of rendered email HTML (Task #115)
# ---------------------------------------------------------------------------


class DirectS3HtmlRewriteTests(unittest.TestCase):
    """The rewrite pass walks every ``<img src>`` / ``<source src>`` URL in
    the rendered email HTML and swaps Replit-hosted URLs (``/api/publish/
    image/...``, ``/api/videos/...``) for direct, long-lived S3 public
    URLs. These tests drive the seam end-to-end without standing up Flask
    or talking to real S3.
    """

    def setUp(self):
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
        from integrations import sendgrid_client
        self._sendgrid = sendgrid_client

    def tearDown(self):
        self._env_patch.stop()

    # -- noop-when-disabled --------------------------------------------------

    def test_rewrite_is_noop_when_s3_backend_disabled(self):
        html = (
            '<img src="/api/publish/image/serve/feat-x" alt="">'
            '<img src="/api/videos/abcdefgh1234/thumb">'
        )
        with mock.patch.object(
            attachment_store, "get_backend_name", return_value="local"
        ):
            out = self._sendgrid.rewrite_email_html_to_direct_s3(html)
        self.assertEqual(out, html)

    # -- hosted-emails -------------------------------------------------------

    def test_rewrite_hosted_image_uses_existing_s3_key(self):
        img_id = "abc123def4567890"
        html = f'<img src="/api/publish/image/hosted/{img_id}" alt="x">'
        with mock.patch.object(
            amp_app, "_load_hosted_image_s3_meta",
            return_value=("hosted-emails/" + img_id + ".png", "", "png"),
        ):
            out = self._sendgrid.rewrite_email_html_to_direct_s3(html)
        self.assertIn(
            f"https://test-bucket.s3.us-east-1.amazonaws.com/hosted-emails/{img_id}.png",
            out,
        )
        self.assertNotIn("/api/publish/image/hosted/", out)

    def test_rewrite_hosted_image_uploads_on_the_fly_when_missing(self):
        img_id = "ff00aa11bb22cc33"
        html = f'<img src="/api/publish/image/hosted/{img_id}" alt="">'
        record = []
        fake = _fake_s3_client(record)
        persisted = []
        with mock.patch.object(
            amp_app, "_load_hosted_image_s3_meta", return_value=None
        ), mock.patch.object(
            amp_app, "_load_hosted_image_db", return_value=("png", _PNG_BYTES)
        ), mock.patch.object(
            amp_app, "_set_hosted_image_s3",
            side_effect=lambda iid, k, u: persisted.append((iid, k, u)) or True,
        ), mock.patch.object(
            attachment_store, "_s3_client", return_value=fake
        ):
            out = self._sendgrid.rewrite_email_html_to_direct_s3(html)
        self.assertEqual(len(record), 1, f"expected 1 S3 PUT, got {record!r}")
        put_kwargs = record[0]
        self.assertEqual(put_kwargs["Bucket"], "test-bucket")
        self.assertTrue(
            put_kwargs["Key"].startswith("hosted-emails/"),
            f"unexpected key {put_kwargs['Key']!r}",
        )
        self.assertEqual(len(persisted), 1)
        self.assertEqual(persisted[0][0], img_id)
        self.assertIn("https://test-bucket.s3.us-east-1.amazonaws.com/", out)
        self.assertNotIn(f"/api/publish/image/hosted/{img_id}", out)

    # -- feature-images ------------------------------------------------------

    def test_rewrite_feature_image_uses_existing_s3_key(self):
        feat = "feat-1"
        html = f'<img src="/api/publish/image/serve/{feat}" alt="">'
        with mock.patch.object(
            publish_store, "get_image",
            return_value={
                "s3_key": "feature-images/feat-1.png",
                "s3_url": "",
                "dataUrl": "",
                "name": "x.png",
            },
        ):
            out = self._sendgrid.rewrite_email_html_to_direct_s3(html)
        self.assertIn(
            "https://test-bucket.s3.us-east-1.amazonaws.com/feature-images/feat-1.png",
            out,
        )

    def test_rewrite_feature_image_uploads_on_the_fly_when_missing(self):
        feat = "feat-2"
        html = f'<img src="/api/publish/image/serve/{feat}">'
        record = []
        persisted = []
        fake = _fake_s3_client(record)
        with mock.patch.object(
            publish_store, "get_image",
            return_value={
                "s3_key": "",
                "s3_url": "",
                "dataUrl": _PNG_DATA_URL,
                "name": "x.png",
                "is_gif": False,
            },
        ), mock.patch.object(
            publish_store, "set_publish_image_s3",
            side_effect=lambda fid, k, u, ct: persisted.append((fid, k, u, ct)) or True,
        ), mock.patch.object(
            attachment_store, "_s3_client", return_value=fake
        ):
            out = self._sendgrid.rewrite_email_html_to_direct_s3(html)
        self.assertEqual(len(record), 1, f"expected 1 S3 PUT, got {record!r}")
        self.assertTrue(record[0]["Key"].startswith("feature-images/"))
        self.assertEqual(len(persisted), 1)
        self.assertEqual(persisted[0][0], feat)
        self.assertIn("https://test-bucket.s3.us-east-1.amazonaws.com/feature-images/", out)

    # -- video-thumbs --------------------------------------------------------

    def test_rewrite_video_thumb_uses_existing_s3_thumb_key(self):
        vid = "abcdefgh1234"
        html = f'<img src="/api/videos/{vid}/thumb" alt="">'
        with mock.patch.object(
            publish_store, "get_video_meta",
            return_value={
                "s3_thumb_key": f"videos/{vid}/thumb.jpg",
                "s3_thumb_url": "",
                "ext": ".mp4",
            },
        ):
            out = self._sendgrid.rewrite_email_html_to_direct_s3(html)
        self.assertIn(
            f"https://test-bucket.s3.us-east-1.amazonaws.com/videos/{vid}/thumb.jpg",
            out,
        )
        self.assertNotIn(f"/api/videos/{vid}/thumb", out)

    def test_rewrite_video_thumb_uploads_on_the_fly_when_missing(self):
        vid = "qrstuvwx9999"
        html = f'<img src="/api/videos/{vid}/thumb">'
        with tempfile.TemporaryDirectory() as tmp:
            thumb_path = os.path.join(tmp, "thumb.jpg")
            with open(thumb_path, "wb") as f:
                f.write(_PNG_BYTES)
            record = []
            persisted = []
            fake = _fake_s3_client(record)
            with mock.patch.object(
                publish_store, "get_video_meta",
                return_value={"s3_thumb_key": "", "s3_thumb_url": "", "ext": ".mp4"},
            ), mock.patch.object(
                publish_store, "get_video_thumb_path", return_value=thumb_path
            ), mock.patch.object(
                publish_store, "set_video_s3_keys",
                side_effect=lambda vid, **kw: persisted.append((vid, kw)) or True,
            ), mock.patch.object(
                attachment_store, "_s3_client", return_value=fake
            ):
                out = self._sendgrid.rewrite_email_html_to_direct_s3(html)
        self.assertEqual(len(record), 1)
        self.assertTrue(record[0]["Key"].startswith("videos/"))
        self.assertIn("https://test-bucket.s3.us-east-1.amazonaws.com/videos/", out)
        self.assertEqual(persisted[0][0], vid)
        self.assertIn("s3_thumb_key", persisted[0][1])

    # -- external-thumbs -----------------------------------------------------

    def test_rewrite_external_thumb_uses_existing_s3_key(self):
        ext_key = "deadbeef"
        html = f'<img src="/api/videos/external-thumb/{ext_key}" alt="">'
        s3_key = f"external-thumbs/{ext_key}.jpg"
        with mock.patch.object(
            video_thumb, "get_external_thumb_s3_key", return_value=s3_key
        ):
            out = self._sendgrid.rewrite_email_html_to_direct_s3(html)
        self.assertIn(
            f"https://test-bucket.s3.us-east-1.amazonaws.com/{s3_key}",
            out,
        )

    # -- graceful per-asset fallback ----------------------------------------

    def test_rewrite_leaves_url_unchanged_when_resolution_fails(self):
        # Hosted image with no s3 meta and no DB row — nothing the rewrite
        # can do. The URL should be left untouched and the rest of the
        # HTML (including a successful sibling rewrite) should still
        # work, proving one bad asset doesn't break the whole render.
        bad = "1111111111111111"
        good = "2222222222222222"
        html = (
            f'<img src="/api/publish/image/hosted/{bad}" alt="bad">'
            f'<img src="/api/publish/image/hosted/{good}" alt="good">'
        )
        def _meta(iid):
            if iid == good:
                return ("hosted-emails/" + good + ".png", "", "png")
            return None
        def _bytes(iid):
            return None  # simulate "no recoverable bytes" for `bad`
        with mock.patch.object(
            amp_app, "_load_hosted_image_s3_meta", side_effect=_meta
        ), mock.patch.object(
            amp_app, "_load_hosted_image_db", side_effect=_bytes
        ):
            out = self._sendgrid.rewrite_email_html_to_direct_s3(html)
        # Bad URL preserved verbatim:
        self.assertIn(f"/api/publish/image/hosted/{bad}", out)
        # Good URL rewritten:
        self.assertIn(
            f"https://test-bucket.s3.us-east-1.amazonaws.com/hosted-emails/{good}.png",
            out,
        )

    # -- video bodies (<source src="/api/videos/<id>">) ---------------------

    def test_rewrite_video_body_uses_existing_s3_key(self):
        vid = "videoid12345"
        # Both <video src=...> and <source src=...> patterns must be rewritten.
        html = (
            f'<video src="/api/videos/{vid}" controls></video>'
            f'<source src="/api/videos/{vid}" type="video/mp4">'
        )
        with mock.patch.object(
            publish_store, "get_video_meta",
            return_value={
                "s3_key": f"videos/{vid}/video.mp4",
                "s3_url": "",
                "ext": ".mp4",
            },
        ):
            out = self._sendgrid.rewrite_email_html_to_direct_s3(html)
        # The <source src=...> match must be rewritten — that's the one the
        # email actually uses. (The non-img/non-source <video src> form is
        # not in our regex, so it stays unchanged; that's fine because no
        # rendered email emits it.)
        self.assertIn(
            f'<source src="https://test-bucket.s3.us-east-1.amazonaws.com/videos/{vid}/video.mp4"',
            out,
        )

    def test_rewrite_video_body_uploads_on_the_fly_when_missing(self):
        vid = "videoid67890"
        html = f'<source src="/api/videos/{vid}" type="video/mp4">'
        with tempfile.TemporaryDirectory() as tmp:
            video_path = os.path.join(tmp, "video.mp4")
            with open(video_path, "wb") as f:
                f.write(b"fake-mp4-bytes")
            record = []
            persisted = []
            fake = _fake_s3_client(record)
            with mock.patch.object(
                publish_store, "get_video_meta",
                return_value={"s3_key": "", "s3_url": "", "ext": ".mp4"},
            ), mock.patch.object(
                publish_store, "get_video_path",
                return_value=(video_path, {"ext": ".mp4"}),
            ), mock.patch.object(
                publish_store, "set_video_s3_keys",
                side_effect=lambda vid, **kw: persisted.append((vid, kw)) or True,
            ), mock.patch.object(
                attachment_store, "_s3_client", return_value=fake
            ):
                out = self._sendgrid.rewrite_email_html_to_direct_s3(html)
        self.assertEqual(len(record), 1)
        self.assertTrue(record[0]["Key"].startswith("videos/"))
        self.assertEqual(record[0]["ContentType"], "video/mp4")
        self.assertEqual(persisted[0][0], vid)
        self.assertIn("s3_key", persisted[0][1])
        self.assertIn("https://test-bucket.s3.us-east-1.amazonaws.com/videos/", out)

    # -- explicit S3 upload failure fallback --------------------------------

    def test_rewrite_leaves_url_unchanged_when_s3_upload_raises(self):
        # Lock in graceful-degradation: when the S3 client itself fails on
        # PUT, the rewrite must NOT raise and must NOT corrupt the URL —
        # it leaves the original /api/... URL in place so the in-app
        # serve route still handles it for the recipient.
        feat = "feat-fail"
        html = f'<img src="/api/publish/image/serve/{feat}">'
        record = []
        # raise_on_put=True makes the fake S3 client raise BotoCoreError
        # inside attachment_store.put — exercising the seam's except branch.
        fake = _fake_s3_client(record, raise_on_put=True)
        persist_calls = []
        with mock.patch.object(
            publish_store, "get_image",
            return_value={
                "s3_key": "",
                "s3_url": "",
                "dataUrl": _PNG_DATA_URL,
                "name": "x.png",
                "is_gif": False,
            },
        ), mock.patch.object(
            publish_store, "set_publish_image_s3",
            side_effect=lambda *a, **kw: persist_calls.append((a, kw)) or True,
        ), mock.patch.object(
            attachment_store, "_s3_client", return_value=fake
        ):
            out = self._sendgrid.rewrite_email_html_to_direct_s3(html)
        # PUT was attempted and failed:
        self.assertEqual(len(record), 1)
        # No persist call — we never got an S3 key back:
        self.assertEqual(persist_calls, [])
        # URL preserved verbatim — the in-app serve route still handles it:
        self.assertIn(f"/api/publish/image/serve/{feat}", out)
        self.assertNotIn("test-bucket.s3.us-east-1.amazonaws.com", out)

    # -- <video poster="..."> future-proofing -------------------------------

    def test_rewrite_video_poster_attribute(self):
        # No rendered email emits <video poster=...> today (we use <img>),
        # but the matcher covers it so any future template addition is
        # already self-contained without another release.
        vid = "abcdefgh1234"
        html = f'<video poster="/api/videos/{vid}/thumb" controls></video>'
        with mock.patch.object(
            publish_store, "get_video_meta",
            return_value={
                "s3_thumb_key": f"videos/{vid}/thumb.jpg",
                "s3_thumb_url": "",
                "ext": ".mp4",
            },
        ):
            out = self._sendgrid.rewrite_email_html_to_direct_s3(html)
        self.assertIn(
            f'poster="https://test-bucket.s3.us-east-1.amazonaws.com/videos/{vid}/thumb.jpg"',
            out,
        )

    # -- non-target URLs left alone -----------------------------------------

    def test_rewrite_does_not_touch_unrelated_urls(self):
        html = (
            '<img src="https://img.youtube.com/vi/abc/hqdefault.jpg">'
            '<a href="/api/videos/something1234">click</a>'
            '<img src="https://test-bucket.s3.us-east-1.amazonaws.com/already.png">'
        )
        out = self._sendgrid.rewrite_email_html_to_direct_s3(html)
        self.assertEqual(out, html)


# ---------------------------------------------------------------------------
# Auto-migration sweep failure-alert (Task #108)
# ---------------------------------------------------------------------------


class _SweepStateGuard:
    """Context manager that snapshots and restores ``_BACKFILL_SWEEP_STATE``.

    The state lives on the module so a test that mutates it would leak
    across tests; this guard makes per-test isolation cheap.
    """

    def __enter__(self):
        with amp_app._BACKFILL_SWEEP_LOCK:
            self._snapshot = dict(amp_app._BACKFILL_SWEEP_STATE)
        return self

    def __exit__(self, exc_type, exc, tb):
        with amp_app._BACKFILL_SWEEP_LOCK:
            amp_app._BACKFILL_SWEEP_STATE.clear()
            amp_app._BACKFILL_SWEEP_STATE.update(self._snapshot)
        return False


class SweepFailureAlertTests(unittest.TestCase):
    """Direct tests of ``_backfill_sweep_record_outcome`` and the
    silence/clear endpoints.

    These don't run a real cycle — they call the recording function the
    way the daemon loop does, so we can exercise the latching behaviour
    deterministically.
    """

    def setUp(self):
        amp_app.app.config["TESTING"] = True
        self.client = amp_app.app.test_client()
        self._env = mock.patch.dict(
            os.environ,
            {
                "AMPLIFY_ADMIN_TOKEN": ADMIN_TOKEN,
                # Threshold of 2 keeps tests fast but proves we don't
                # alert on a single bad cycle.
                "AMPLIFY_BACKFILL_ALERT_THRESHOLD": "2",
                # Webhook intentionally unset so we exercise the
                # "dashboard-only" path without hitting the network.
                "AMPLIFY_BACKFILL_ALERT_WEBHOOK": "",
            },
            clear=False,
        )
        self._env.start()
        self._guard = _SweepStateGuard().__enter__()
        # Reset alert-tracking fields to a clean baseline.
        with amp_app._BACKFILL_SWEEP_LOCK:
            amp_app._BACKFILL_SWEEP_STATE.update({
                "alert_threshold": 2,
                "consecutive_failures": 0,
                "consecutive_successes": 0,
                "alert_active": False,
                "alert_first_seen_at": None,
                "alert_last_kind_errors": None,
                "webhook_in_incident": False,
                "silenced_until": None,
                "last_failure_at": None,
                "last_recovery_at": None,
                "last_error": None,
            })

    def tearDown(self):
        self._guard.__exit__(None, None, None)
        self._env.stop()

    def _record(self, failed, *, last_error=None, kind_errors=None):
        """Convenience wrapper around the production recorder."""
        report = {}
        totals = {"scanned": 0, "uploaded": 0, "errors": 1 if failed else 0}
        if kind_errors:
            for k, n in kind_errors.items():
                report[k] = {"errors": n}
                totals["errors"] = max(totals["errors"], n)
        return amp_app._backfill_sweep_record_outcome(
            failed=failed,
            last_error=last_error,
            report=report,
            totals=totals,
            finished_at=time.time(),
        )

    # --- threshold / latching ---------------------------------------------

    def test_dashboard_alert_does_not_latch_below_threshold(self):
        # Threshold is 2 — a single failed cycle MUST NOT light up the
        # dashboard banner, but it SHOULD ping the webhook (per the
        # task spec: "pinged on the first failing cycle").
        kind, payload = self._record(failed=True, last_error="boom")
        self.assertEqual(kind, "firing")
        self.assertEqual(payload["consecutive_failures"], 1)
        snap = amp_app._backfill_sweep_status_snapshot()
        self.assertFalse(snap["alert"]["active"])
        self.assertFalse(snap["alert"]["visible"])
        self.assertTrue(snap["alert"]["webhook_in_incident"])
        self.assertEqual(snap["alert"]["consecutive_failures"], 1)

    def test_dashboard_alert_latches_at_threshold(self):
        # First failure pings the webhook; second failure latches the
        # dashboard banner without re-firing the webhook.
        kind1, _ = self._record(failed=True, last_error="boom1")
        self.assertEqual(kind1, "firing")
        kind2, payload2 = self._record(
            failed=True, last_error="boom2", kind_errors={"videos": 3, "feature-images": 1}
        )
        self.assertIsNone(kind2)  # webhook silent — already firing
        self.assertIsNone(payload2)
        snap = amp_app._backfill_sweep_status_snapshot()
        self.assertTrue(snap["alert"]["active"])
        self.assertTrue(snap["alert"]["visible"])
        self.assertEqual(snap["alert"]["consecutive_failures"], 2)
        self.assertEqual(snap["alert"]["kind_errors"], {"videos": 3, "feature-images": 1})

    def test_webhook_fires_only_once_per_incident(self):
        first, _ = self._record(failed=True)
        self.assertEqual(first, "firing")
        # Subsequent failures in the same incident must not re-fire to
        # avoid notification spam.
        second, _ = self._record(failed=True)
        self.assertIsNone(second)
        third, _ = self._record(failed=True)
        self.assertIsNone(third)
        snap = amp_app._backfill_sweep_status_snapshot()
        self.assertEqual(snap["alert"]["consecutive_failures"], 3)
        self.assertTrue(snap["alert"]["webhook_in_incident"])

    def test_recovery_emits_resolved_and_clears_alert(self):
        self._record(failed=True)
        self._record(failed=True)  # latches dashboard alert
        kind, payload = self._record(failed=False)
        self.assertEqual(kind, "resolved")
        self.assertIn("recovered_at", payload)
        snap = amp_app._backfill_sweep_status_snapshot()
        self.assertFalse(snap["alert"]["active"])
        self.assertFalse(snap["alert"]["webhook_in_incident"])
        self.assertEqual(snap["alert"]["consecutive_failures"], 0)
        self.assertIsNotNone(snap["alert"]["last_recovery_at"])

    def test_recovery_emits_resolved_even_when_dashboard_never_latched(self):
        # Single failure → webhook fires but dashboard stays quiet (we
        # never reached threshold). Recovery still owes the webhook a
        # "resolved" ping so the on-call channel sees the incident close.
        first, _ = self._record(failed=True)
        self.assertEqual(first, "firing")
        kind, payload = self._record(failed=False)
        self.assertEqual(kind, "resolved")
        self.assertIn("recovered_at", payload)
        snap = amp_app._backfill_sweep_status_snapshot()
        self.assertFalse(snap["alert"]["webhook_in_incident"])

    def test_clean_cycle_without_prior_alert_does_not_emit(self):
        kind, payload = self._record(failed=False)
        self.assertIsNone(kind)
        self.assertIsNone(payload)

    def test_failed_cycle_with_only_last_error_still_counts(self):
        # totals.errors=0 but the daemon recorded a non-null last_error
        # (e.g. crashed before per-kind counters were updated). The
        # task spec defines this as a failed cycle.
        kind, payload = amp_app._backfill_sweep_record_outcome(
            failed=True,  # mirrors what the loop computes from last_error
            last_error="bucket policy denied PutObject",
            report={},
            totals={"scanned": 0, "uploaded": 0, "errors": 0},
            finished_at=time.time(),
        )
        self.assertEqual(kind, "firing")
        self.assertEqual(payload["last_error"], "bucket policy denied PutObject")
        snap = amp_app._backfill_sweep_status_snapshot()
        self.assertEqual(snap["alert"]["consecutive_failures"], 1)

    # --- silence / clear endpoints ----------------------------------------

    def test_silence_endpoint_hides_alert_until_window_elapses(self):
        self._record(failed=True)
        self._record(failed=True)
        snap = amp_app._backfill_sweep_status_snapshot()
        self.assertTrue(snap["alert"]["visible"])

        r = self.client.post(
            "/api/admin/attachments/sweep/silence",
            json={"minutes": 30, "admin_token": ADMIN_TOKEN},
        )
        self.assertEqual(r.status_code, 200, r.data)
        body = r.get_json()
        self.assertTrue(body["success"])
        self.assertEqual(body["minutes"], 30)
        self.assertGreater(body["silenced_until"], time.time())

        snap = amp_app._backfill_sweep_status_snapshot()
        self.assertTrue(snap["alert"]["active"])
        self.assertTrue(snap["alert"]["silenced"])
        self.assertFalse(snap["alert"]["visible"])

        # Snap forward: pretend the silence window already elapsed; the
        # snapshot should auto-clear it and re-surface the alert.
        with amp_app._BACKFILL_SWEEP_LOCK:
            amp_app._BACKFILL_SWEEP_STATE["silenced_until"] = time.time() - 1
        snap = amp_app._backfill_sweep_status_snapshot()
        self.assertFalse(snap["alert"]["silenced"])
        self.assertTrue(snap["alert"]["visible"])
        self.assertIsNone(snap["alert"]["silenced_until"])

    def test_silence_clamps_minutes_to_safe_window(self):
        # Negative / zero / huge values must not be accepted as-is, so
        # an operator can't accidentally silence the alert "forever".
        for raw, expected in [(0, 1), (-5, 1), (10**9, 24 * 60)]:
            r = self.client.post(
                "/api/admin/attachments/sweep/silence",
                json={"minutes": raw, "admin_token": ADMIN_TOKEN},
            )
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.get_json()["minutes"], expected)

    def test_silence_requires_admin_token(self):
        r = self.client.post(
            "/api/admin/attachments/sweep/silence",
            json={"minutes": 30},
        )
        self.assertEqual(r.status_code, 401)

    def test_clear_endpoint_resets_alert_and_silence(self):
        self._record(failed=True)
        self._record(failed=True)
        # Silence first so we can prove the clear drops it too.
        self.client.post(
            "/api/admin/attachments/sweep/silence",
            json={"minutes": 60, "admin_token": ADMIN_TOKEN},
        )

        r = self.client.post(
            "/api/admin/attachments/sweep/clear",
            json={"admin_token": ADMIN_TOKEN},
        )
        self.assertEqual(r.status_code, 200, r.data)
        body = r.get_json()
        self.assertTrue(body["success"])
        self.assertTrue(body["was_active"])

        snap = amp_app._backfill_sweep_status_snapshot()
        self.assertFalse(snap["alert"]["active"])
        self.assertFalse(snap["alert"]["silenced"])
        self.assertEqual(snap["alert"]["consecutive_failures"], 0)
        self.assertIsNone(snap["alert"]["silenced_until"])

    def test_clear_requires_admin_token(self):
        r = self.client.post("/api/admin/attachments/sweep/clear", json={})
        self.assertEqual(r.status_code, 401)

    # --- webhook dispatch --------------------------------------------------

    def test_webhook_called_on_firing_and_resolved_when_configured(self):
        sent: list = []

        def fake_urlopen(req, timeout=5):
            sent.append({
                "url": req.full_url,
                "data": req.data,
                "method": req.get_method(),
            })

            class _R:
                status = 200
                def read(self_inner):
                    return b""
                def __enter__(self_inner):
                    return self_inner
                def __exit__(self_inner, *a):
                    return False
            return _R()

        with mock.patch.dict(
            os.environ,
            {"AMPLIFY_BACKFILL_ALERT_WEBHOOK": "https://hooks.example.com/sweep"},
        ), mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            # First failure pings firing immediately (per task spec).
            kind, payload = self._record(failed=True)
            self.assertEqual(kind, "firing")
            amp_app._backfill_sweep_post_webhook(payload)

            # Recovery on the very next cycle still pings resolved.
            kind, payload = self._record(failed=False)
            self.assertEqual(kind, "resolved")
            amp_app._backfill_sweep_post_webhook(payload)

        self.assertEqual(len(sent), 2)
        firing_body = json.loads(sent[0]["data"].decode("utf-8"))
        resolved_body = json.loads(sent[1]["data"].decode("utf-8"))
        self.assertEqual(firing_body["kind"], "firing")
        self.assertEqual(resolved_body["kind"], "resolved")
        self.assertIn("text", firing_body)
        self.assertIn("text", resolved_body)

    def test_webhook_swallows_exceptions(self):
        def boom(*a, **kw):
            raise RuntimeError("network down")

        with mock.patch.dict(
            os.environ,
            {"AMPLIFY_BACKFILL_ALERT_WEBHOOK": "https://hooks.example.com/sweep"},
        ), mock.patch("urllib.request.urlopen", side_effect=boom):
            # Must not raise — operators would lose the alert otherwise.
            amp_app._backfill_sweep_post_webhook({"kind": "firing", "text": "x"})
        snap = amp_app._backfill_sweep_status_snapshot()
        self.assertEqual(snap["alert"]["last_notification_kind"], "firing")
        self.assertIn("network down", snap["alert"]["last_notification_error"] or "")

    def test_webhook_skipped_when_url_unset(self):
        # Already cleared via setUp env. urlopen would fail loudly if
        # called, so this implicitly asserts we early-return.
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("should not be called")):
            amp_app._backfill_sweep_post_webhook({"kind": "firing", "text": "x"})

    # --- status endpoint shape --------------------------------------------

    def test_status_endpoint_includes_alert_block(self):
        with mock.patch.object(
            amp_app,
            "_attachments_pending_counts",
            return_value={
                "feature-images": 0, "videos": 0, "video-thumbs": 0,
                "external-thumbs": 0, "hosted-emails": 0, "announcements": 0,
            },
        ):
            r = self.client.get(
                "/api/admin/attachments/status",
                headers={"X-Admin-Token": ADMIN_TOKEN},
            )
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertIn("auto_sweep", body)
        self.assertIn("alert", body["auto_sweep"])
        for key in (
            "active", "visible", "silenced", "threshold",
            "consecutive_failures", "webhook_configured",
        ):
            self.assertIn(key, body["auto_sweep"]["alert"])


if __name__ == "__main__":
    unittest.main()
