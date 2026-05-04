"""End-to-end smoke tests for the Chartmetric announcement live wiring (Task #143).

These tests verify the *exact* payload shape Amplify sends to the Chartmetric
admin REST API once the live wiring is enabled — namely:

  * Amplify-only fields (display_format / scheduled_publish_at / source_*) are
    stripped before the wire call.
  * The local-id ↔ Chartmetric-id mapping is persisted on the working copy.
  * Categories are auto-resolved by name (case-insensitive); missing names
    are POSTed first so the post create body carries the freshly minted
    Chartmetric ids.
  * The link-table replace endpoint (PUT /admin/announcement/<id>/categories)
    is hit immediately after every successful post create / update.
  * Boost toggles on a synced + published post fan out to
    PATCH /admin/announcement/<id>/boost without touching anything else.
  * The legacy ``publish_announcement_quick`` shim still works.

The Chartmetric REST client (``integrations.chartmetric_announcement_client``)
is fully exercised, but no real network traffic is issued — ``requests.request``
is monkey-patched with a recording fake.

Run with:
    cd artifacts/amplify && python -m unittest tests.test_chartmetric_announcement_client
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_AMPLIFY_DIR = os.path.dirname(_HERE)
if _AMPLIFY_DIR not in sys.path:
    sys.path.insert(0, _AMPLIFY_DIR)

# Force live mode BEFORE importing the store — live mode now activates
# automatically when both env vars are set (no kill switch needed).
os.environ["CHARTMETRIC_ADMIN_API_BASE_URL"] = "https://api.chartmetric.test"
os.environ["CHARTMETRIC_ADMIN_API_TOKEN"] = "test-token"
os.environ.pop("ANNOUNCEMENTS_STUB_MODE", None)

import config  # noqa: E402
config.CHARTMETRIC_ADMIN_API_BASE_URL = "https://api.chartmetric.test"
config.CHARTMETRIC_ADMIN_API_TOKEN = "test-token"

from ai import announcement_store  # noqa: E402
from integrations import chartmetric_announcement_client as cmc  # noqa: E402


# ---------------------------------------------------------------------------
# Fake transport
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status_code: int, body):
        self.status_code = status_code
        self._body = body
        self.content = b"x" if body is not None else b""
        self.text = json.dumps(body) if isinstance(body, (dict, list)) else (body or "")

    def json(self):
        if isinstance(self._body, (dict, list)):
            return self._body
        raise ValueError("not JSON")


class FakeChartmetric:
    """Records every HTTP call and dispatches to handcrafted responses."""

    def __init__(self):
        self.calls: list[dict] = []
        self.next_post_id = 100
        self.next_cat_id = 200
        self.posts: dict[int, dict] = {}
        self.categories: dict[int, dict] = {}
        self.links: dict[int, list[int]] = {}

    def __call__(self, method, url, *, params=None, json=None, headers=None,
                 timeout=None, **kwargs):
        path = url.replace("https://api.chartmetric.test", "")
        self.calls.append({"method": method.upper(), "path": path,
                           "json": json, "params": params,
                           "headers": dict(headers or {})})
        return self._dispatch(method.upper(), path, json or {})

    # -- routing --------------------------------------------------------
    def _dispatch(self, method, path, body):
        if path == "/admin/announcement/categories":
            if method == "GET":
                return _FakeResponse(200, list(self.categories.values()))
            if method == "POST":
                cid = self.next_cat_id
                self.next_cat_id += 1
                cat = {"id": cid, **body}
                self.categories[cid] = cat
                return _FakeResponse(201, cat)
        if path.startswith("/admin/announcement/categories/"):
            cid = int(path.rsplit("/", 1)[-1])
            if method == "PUT":
                if cid not in self.categories:
                    return _FakeResponse(404, {"error": "not found"})
                self.categories[cid].update(body)
                return _FakeResponse(200, self.categories[cid])
            if method == "DELETE":
                self.categories.pop(cid, None)
                return _FakeResponse(204, None)
        if path == "/admin/announcement":
            if method == "POST":
                pid = self.next_post_id
                self.next_post_id += 1
                row = {"id": pid, **body}
                self.posts[pid] = row
                return _FakeResponse(201, row)
            if method == "GET":
                return _FakeResponse(200, {"items": list(self.posts.values()),
                                            "total": len(self.posts)})
        if path.startswith("/admin/announcement/") and path.endswith("/boost"):
            pid = int(path.split("/")[-2])
            if method == "PATCH":
                if pid not in self.posts:
                    return _FakeResponse(404, {"error": "not found"})
                self.posts[pid]["is_boosted"] = bool(body.get("is_boosted"))
                return _FakeResponse(200, {"id": pid,
                                            "is_boosted": self.posts[pid]["is_boosted"]})
        if path.startswith("/admin/announcement/") and path.endswith("/categories"):
            pid = int(path.split("/")[-2])
            if method == "PUT":
                ids = body.get("category_ids") or []
                self.links[pid] = list(ids)
                return _FakeResponse(200, {"id": pid, "category_ids": ids})
        # Fall-through: detail / update / delete on a post.
        parts = path.split("/")
        if len(parts) == 4 and parts[:3] == ["", "admin", "announcement"]:
            try:
                pid = int(parts[3])
            except ValueError:
                pid = None
            if pid is not None:
                if method == "GET":
                    if pid not in self.posts:
                        return _FakeResponse(404, {"error": "not found"})
                    return _FakeResponse(200, self.posts[pid])
                if method == "PUT":
                    if pid not in self.posts:
                        return _FakeResponse(404, {"error": "not found"})
                    self.posts[pid].update(body)
                    return _FakeResponse(200, self.posts[pid])
                if method == "DELETE":
                    self.posts.pop(pid, None)
                    self.links.pop(pid, None)
                    return _FakeResponse(204, None)
        return _FakeResponse(500, {"error": f"no route for {method} {path}"})


# ---------------------------------------------------------------------------
# Test base
# ---------------------------------------------------------------------------

class _LiveModeTestCase(unittest.TestCase):
    """Each test gets a fresh JSON store + fresh fake Chartmetric."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="amp-ann-")
        self._orig_store_file = announcement_store._STORE_FILE
        announcement_store._STORE_FILE = os.path.join(self._tmp, "store.json")
        self.fake = FakeChartmetric()
        self._patcher = mock.patch("requests.request", side_effect=self.fake)
        self._patcher.start()
        self.addCleanup(self._patcher.stop)
        self.addCleanup(self._cleanup_store)

    def _cleanup_store(self):
        announcement_store._STORE_FILE = self._orig_store_file
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _calls(self, method=None, path_contains=None):
        out = self.fake.calls
        if method:
            out = [c for c in out if c["method"] == method]
        if path_contains:
            out = [c for c in out if path_contains in c["path"]]
        return out


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class LiveModeDetectionTests(_LiveModeTestCase):

    def test_live_mode_is_active(self):
        self.assertFalse(announcement_store._stub_mode_enabled())
        self.assertTrue(announcement_store._live_mode_enabled())


class ModeSwitchTests(unittest.TestCase):
    """Mode is decided per-call from env + config; verify each combo."""

    def setUp(self):
        self._orig_base = config.CHARTMETRIC_ADMIN_API_BASE_URL
        self._orig_token = config.CHARTMETRIC_ADMIN_API_TOKEN
        self._orig_env = os.environ.get("ANNOUNCEMENTS_STUB_MODE")

    def tearDown(self):
        config.CHARTMETRIC_ADMIN_API_BASE_URL = self._orig_base
        config.CHARTMETRIC_ADMIN_API_TOKEN = self._orig_token
        if self._orig_env is None:
            os.environ.pop("ANNOUNCEMENTS_STUB_MODE", None)
        else:
            os.environ["ANNOUNCEMENTS_STUB_MODE"] = self._orig_env

    def _set(self, base, token, switch=None):
        config.CHARTMETRIC_ADMIN_API_BASE_URL = base
        config.CHARTMETRIC_ADMIN_API_TOKEN = token
        if switch is None:
            os.environ.pop("ANNOUNCEMENTS_STUB_MODE", None)
        else:
            os.environ["ANNOUNCEMENTS_STUB_MODE"] = switch

    def test_both_env_vars_set_enables_live_mode_automatically(self):
        self._set("https://api.example.com", "tok")
        self.assertFalse(announcement_store._stub_mode_enabled())

    def test_missing_base_url_forces_stub(self):
        self._set("", "tok")
        self.assertTrue(announcement_store._stub_mode_enabled())

    def test_missing_token_forces_stub(self):
        self._set("https://api.example.com", "")
        self.assertTrue(announcement_store._stub_mode_enabled())

    def test_kill_switch_pins_to_stub_even_with_live_env(self):
        self._set("https://api.example.com", "tok", switch="true")
        self.assertTrue(announcement_store._stub_mode_enabled())

    def test_kill_switch_falsy_does_not_force_stub(self):
        self._set("https://api.example.com", "tok", switch="false")
        self.assertFalse(announcement_store._stub_mode_enabled())


class TranslationValidationTests(unittest.TestCase):

    def test_rejects_en_key(self):
        with self.assertRaises(announcement_store.ValidationError) as ctx:
            announcement_store._validate_translations(
                {"en": {"title": "x", "content": [{"type": "p"}]}},
                ("title", "content"))
        self.assertIn("'en'", str(ctx.exception))

    def test_rejects_bad_content_shape(self):
        with self.assertRaises(announcement_store.ValidationError):
            announcement_store._validate_translations(
                {"de": {"title": "ok", "content": "not a list"}},
                ("title", "content"))

    def test_rejects_empty_title(self):
        with self.assertRaises(announcement_store.ValidationError):
            announcement_store._validate_translations(
                {"de": {"title": "", "content": [{"type": "p"}]}},
                ("title", "content"))

    def test_rejects_partial_post_locale_missing_content(self):
        with self.assertRaises(announcement_store.ValidationError) as ctx:
            announcement_store._validate_translations(
                {"de": {"title": "ok"}}, ("title", "content"))
        self.assertIn("content", str(ctx.exception))

    def test_rejects_partial_post_locale_missing_title(self):
        with self.assertRaises(announcement_store.ValidationError) as ctx:
            announcement_store._validate_translations(
                {"de": {"content": [{"type": "p"}]}}, ("title", "content"))
        self.assertIn("title", str(ctx.exception))

    def test_rejects_category_locale_missing_name(self):
        with self.assertRaises(announcement_store.ValidationError) as ctx:
            announcement_store._validate_translations({"de": {}}, ("name",))
        self.assertIn("name", str(ctx.exception))

    def test_accepts_full_post_locale(self):
        out = announcement_store._validate_translations(
            {"de": {"title": "Hallo",
                    "content": [{"type": "paragraph",
                                 "children": [{"text": "Welt"}]}]}},
            ("title", "content"))
        self.assertIn("de", out)
        self.assertEqual(out["de"]["title"], "Hallo")

    def test_silently_drops_unknown_locale(self):
        out = announcement_store._validate_translations(
            {"xx": {"title": "x"}}, ("title", "content"))
        self.assertEqual(out, {})


class CreatePostFlowTests(_LiveModeTestCase):

    def test_create_post_strips_amplify_only_fields_and_pushes(self):
        # Seed local categories first (auto-seeds on first call).
        announcement_store.list_categories()
        cats = announcement_store.list_categories()
        cat_id = cats[0]["id"]

        post = announcement_store.create_post({
            "title": "Hello live world",
            "content": [{"type": "paragraph",
                         "children": [{"text": "Body"}]}],
            "translations": {"de": {"title": "Hallo",
                                     "content": [{"type": "paragraph",
                                                  "children": [{"text": "Welt"}]}]}},
            "category_ids": [cat_id],
            "image_url": None,
            "display_format": "popup",
            "is_pinned": True,
            "is_boosted": False,
            "status": "publish_now",
            "scheduled_publish_at": None,
            "source_feature_id": "ABC123",
            "source_feature_set_id": "set-7",
        })

        # 1. Amplify-side: chartmetric_id wired up, Amplify-only fields kept locally.
        self.assertEqual(post["display_format"], "popup")
        self.assertEqual(post["source_feature_id"], "ABC123")
        self.assertIsNotNone(post["chartmetric_id"])

        # 2. The push to Chartmetric must have a clean payload — NO Amplify-only fields.
        post_creates = self._calls(method="POST", path_contains="/admin/announcement")
        post_creates = [c for c in post_creates if c["path"] == "/admin/announcement"]
        self.assertEqual(len(post_creates), 1, "Expected exactly one post-create call")
        body = post_creates[0]["json"]
        self.assertNotIn("display_format", body)
        self.assertNotIn("scheduled_publish_at", body)
        self.assertNotIn("source_feature_id", body)
        self.assertNotIn("source_feature_set_id", body)
        self.assertNotIn("status", body, "Local 'status' enum must not leak to wire")
        self.assertEqual(body["title"], "Hello live world")
        self.assertTrue(body["is_published"])
        self.assertTrue(body["is_pinned"])

        # 3. Translations passed through; no "en" key.
        self.assertNotIn("en", body["translations"])
        self.assertIn("de", body["translations"])

        # 4. Category was auto-created on Chartmetric and its id was used in the body.
        cat_creates = self._calls(method="POST",
                                   path_contains="/admin/announcement/categories")
        self.assertEqual(len(cat_creates), 1)
        remote_cat_id = list(self.fake.categories.keys())[0]
        self.assertEqual(body["category_ids"], [remote_cat_id])

        # 5. Link-table replace fired right after.
        link_calls = self._calls(method="PUT", path_contains="/categories")
        link_calls = [c for c in link_calls
                      if c["path"].startswith("/admin/announcement/")
                      and c["path"].endswith("/categories")
                      and c["path"].count("/") == 4]
        self.assertEqual(len(link_calls), 1)
        self.assertEqual(link_calls[0]["json"], {"category_ids": [remote_cat_id]})

        # 6. Auth header on every call.
        for c in self.fake.calls:
            self.assertEqual(c["headers"].get("Authorization"), "Bearer test-token")

    def test_second_post_reuses_existing_chartmetric_category(self):
        announcement_store.list_categories()
        cat_id = announcement_store.list_categories()[0]["id"]

        announcement_store.create_post({
            "title": "First", "content": [{"type": "p"}],
            "category_ids": [cat_id], "status": "draft",
            "translations": {},
        })
        first_cat_creates = len([c for c in self.fake.calls
                                  if c["method"] == "POST"
                                  and c["path"] == "/admin/announcement/categories"])
        announcement_store.create_post({
            "title": "Second", "content": [{"type": "p"}],
            "category_ids": [cat_id], "status": "draft",
            "translations": {},
        })
        second_cat_creates = len([c for c in self.fake.calls
                                   if c["method"] == "POST"
                                   and c["path"] == "/admin/announcement/categories"])
        # No additional category-create — the cached chartmetric_id was reused.
        self.assertEqual(first_cat_creates, 1)
        self.assertEqual(second_cat_creates, 1)


class TransactionalWriteTests(_LiveModeTestCase):

    def test_create_post_failure_rolls_back_local_store(self):
        """If the Chartmetric push fails, NOTHING about the failed save
        should land in the on-disk working copy — no orphan post, no
        category chartmetric_id mapping."""
        announcement_store.list_categories()
        cat_id = announcement_store.list_categories()[0]["id"]
        # Snapshot the on-disk store before the failed save.
        with open(announcement_store._STORE_FILE) as f:
            before = json.load(f)

        # Force the post-create POST to 500.
        original = self.fake._dispatch
        def boom(method, path, body):
            if method == "POST" and path == "/admin/announcement":
                return _FakeResponse(500, {"error": "kaboom"})
            return original(method, path, body)
        self.fake._dispatch = boom

        with self.assertRaises(announcement_store.ValidationError) as ctx:
            announcement_store.create_post({
                "title": "Will fail", "content": [{"type": "p"}],
                "category_ids": [cat_id], "status": "draft", "translations": {},
            })
        self.assertEqual(ctx.exception.code, "chartmetric_error")

        with open(announcement_store._STORE_FILE) as f:
            after = json.load(f)
        # On-disk store unchanged: no new post, category mapping not
        # persisted (categories may have been pre-resolved on Chartmetric
        # but the local record's chartmetric_id must not have been saved
        # because the parent _save() never ran).
        self.assertEqual(before["posts"], after["posts"])
        for cid, c in after["categories"].items():
            self.assertEqual(c.get("chartmetric_id"),
                             before["categories"][cid].get("chartmetric_id"))

    def test_multi_category_save_lists_categories_at_most_once(self):
        """A single save touching N categories must trigger at most one
        GET /admin/announcement/categories — the resolver is request-
        scoped so it does not hammer the API."""
        announcement_store.list_categories()
        cats = announcement_store.list_categories()
        ids = [c["id"] for c in cats[:3]]
        self.fake.calls.clear()

        announcement_store.create_post({
            "title": "Many cats", "content": [{"type": "p"}],
            "category_ids": ids, "status": "draft", "translations": {},
        })
        list_calls = [c for c in self.fake.calls
                       if c["method"] == "GET"
                       and c["path"] == "/admin/announcement/categories"]
        self.assertLessEqual(len(list_calls), 1,
                              f"Expected at most 1 GET /categories per save; got {len(list_calls)}")


class UpdatePostFlowTests(_LiveModeTestCase):

    def test_update_changes_categories_and_replays_link_table(self):
        announcement_store.list_categories()
        cats = announcement_store.list_categories()
        a, b = cats[0]["id"], cats[1]["id"]

        post = announcement_store.create_post({
            "title": "T", "content": [{"type": "p"}],
            "category_ids": [a], "status": "draft", "translations": {},
        })
        self.fake.calls.clear()

        announcement_store.update_post(post["id"], {
            "title": "T", "content": [{"type": "p"}],
            "category_ids": [b], "status": "draft", "translations": {},
        })

        # An update must PUT the post then PUT the link table — the link
        # call is the source-of-truth for category replacement (drops `a`,
        # adds the remote id for `b`).
        link_calls = [c for c in self.fake.calls
                      if c["method"] == "PUT"
                      and c["path"].endswith("/categories")
                      and c["path"].count("/") == 4]
        self.assertEqual(len(link_calls), 1)
        # The body should contain exactly one (different) remote id.
        self.assertEqual(len(link_calls[0]["json"]["category_ids"]), 1)


class BoostToggleTests(_LiveModeTestCase):

    def test_boost_on_published_post_fires_patch(self):
        announcement_store.list_categories()
        post = announcement_store.create_post({
            "title": "Live one", "content": [{"type": "p"}],
            "category_ids": [], "status": "publish_now", "translations": {},
        })
        self.fake.calls.clear()

        announcement_store.set_post_boost(post["id"], True)

        patches = [c for c in self.fake.calls if c["method"] == "PATCH"]
        self.assertEqual(len(patches), 1)
        self.assertTrue(patches[0]["path"].endswith("/boost"))
        self.assertEqual(patches[0]["json"], {"is_boosted": True})

    def test_boost_on_draft_is_rejected(self):
        """Draft / scheduled posts must not be boost-toggled live —
        the marketer should save the boost flag from the editor form."""
        announcement_store.list_categories()
        post = announcement_store.create_post({
            "title": "Draft one", "content": [{"type": "p"}],
            "category_ids": [], "status": "draft", "translations": {},
        })
        self.fake.calls.clear()
        with self.assertRaises(announcement_store.ValidationError) as ctx:
            announcement_store.set_post_boost(post["id"], True)
        self.assertEqual(ctx.exception.code, "boost_not_allowed")
        self.assertEqual(ctx.exception.status_code, 409)
        # Nothing crossed the wire.
        self.assertEqual([c for c in self.fake.calls if c["method"] == "PATCH"], [])
        # Local store untouched.
        unchanged = announcement_store.get_post(post["id"])
        self.assertFalse(unchanged["is_boosted"])


class DeleteFlowTests(_LiveModeTestCase):

    def test_delete_post_calls_chartmetric_with_remote_id(self):
        announcement_store.list_categories()
        post = announcement_store.create_post({
            "title": "doomed", "content": [{"type": "p"}],
            "category_ids": [], "status": "draft", "translations": {},
        })
        remote_id = post["chartmetric_id"]
        self.fake.calls.clear()
        ok = announcement_store.delete_post(post["id"])
        self.assertTrue(ok)
        deletes = [c for c in self.fake.calls if c["method"] == "DELETE"]
        self.assertEqual(len(deletes), 1)
        self.assertEqual(deletes[0]["path"], f"/admin/announcement/{remote_id}")


class LegacyPublishQuickTests(_LiveModeTestCase):

    def test_publish_announcement_quick_uses_live_path(self):
        announcement_store.list_categories()
        result = announcement_store.publish_announcement_quick(
            title="Quick publish", body="<p>hi there</p>",
            feature_id="FEAT-1", category="New Feature")
        self.assertTrue(result["success"], msg=result)
        # The shim must end up POSTing to /admin/announcement.
        post_creates = [c for c in self.fake.calls
                         if c["method"] == "POST"
                         and c["path"] == "/admin/announcement"]
        self.assertEqual(len(post_creates), 1)
        body = post_creates[0]["json"]
        self.assertEqual(body["title"], "Quick publish")
        # source_feature_id is Amplify-only and must NOT be sent.
        self.assertNotIn("source_feature_id", body)


class PingTests(_LiveModeTestCase):

    def test_ping_returns_status_and_count(self):
        # Seed two categories on the fake side
        self.fake.categories[1] = {"id": 1, "name": "A"}
        self.fake.categories[2] = {"id": 2, "name": "B"}
        info = announcement_store.ping_chartmetric()
        self.assertTrue(info["ok"], msg=info)
        self.assertEqual(info["status"], 200)
        self.assertEqual(info["count"], 2)
        self.assertFalse(info["stub_mode"])


if __name__ == "__main__":
    unittest.main()
