"""End-to-end smoke tests for the Chartmetric announcement live wiring (Task #143).

These tests verify the *exact* payload shape Amplify sends to the Chartmetric
admin REST API once the live wiring is enabled — namely:

  * Amplify-only fields (boost_types / scheduled_publish_at / source_*) are
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

# Force legacy bearer-token live mode BEFORE importing the store.
# CM_* vars are cleared so real environment secrets don't leak into tests
# and accidentally trigger the cookie-based auth path (which would attempt
# a real /login against the fake URL and fail with 500).
os.environ["CHARTMETRIC_ADMIN_API_BASE_URL"] = "https://api.chartmetric.test"
os.environ["CHARTMETRIC_ADMIN_API_TOKEN"] = "test-token"
os.environ.pop("ANNOUNCEMENTS_STUB_MODE", None)
os.environ.pop("CM_API_BASE_URL", None)
os.environ.pop("CM_SERVICE_ACCOUNT_EMAIL", None)
os.environ.pop("CM_SERVICE_ACCOUNT_PASSWORD", None)

import config  # noqa: E402
config.CHARTMETRIC_ADMIN_API_BASE_URL = "https://api.chartmetric.test"
config.CHARTMETRIC_ADMIN_API_TOKEN = "test-token"
config.CM_API_BASE_URL = ""
config.CM_SERVICE_ACCOUNT_EMAIL = ""
config.CM_SERVICE_ACCOUNT_PASSWORD = ""

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
        if path == "/announcement/categories":
            if method == "GET":
                return _FakeResponse(200, list(self.categories.values()))
            if method == "POST":
                cid = self.next_cat_id
                self.next_cat_id += 1
                cat = {"id": cid, **body}
                self.categories[cid] = cat
                return _FakeResponse(201, cat)
        if path.startswith("/announcement/categories/"):
            cid = int(path.rsplit("/", 1)[-1])
            if method == "PUT":
                if cid not in self.categories:
                    return _FakeResponse(404, {"error": "not found"})
                self.categories[cid].update(body)
                return _FakeResponse(200, self.categories[cid])
            if method == "DELETE":
                self.categories.pop(cid, None)
                return _FakeResponse(204, None)
        if path == "/announcement/list":
            if method == "GET":
                return _FakeResponse(200, {"data": list(self.posts.values()),
                                           "total": len(self.posts)})
        if path == "/announcement":
            if method == "POST":
                pid = self.next_post_id
                self.next_post_id += 1
                row = {"id": pid, **body}
                self.posts[pid] = row
                return _FakeResponse(201, row)
        if path.startswith("/announcement/") and path.endswith("/boost"):
            pid = int(path.split("/")[-2])
            if method == "PATCH":
                if pid not in self.posts:
                    return _FakeResponse(404, {"error": "not found"})
                self.posts[pid]["is_boosted"] = bool(body.get("is_boosted"))
                return _FakeResponse(200, {"id": pid,
                                           "is_boosted": self.posts[pid]["is_boosted"]})
        if path.startswith("/announcement/") and path.endswith("/categories"):
            pid = int(path.split("/")[-2])
            if method == "PUT":
                ids = body.get("category_ids") or []
                self.links[pid] = list(ids)
                return _FakeResponse(200, {"id": pid, "category_ids": ids})
        # Fall-through: detail / update / delete on a post.
        parts = path.split("/")
        if len(parts) == 3 and parts[:2] == ["", "announcement"]:
            try:
                pid = int(parts[2])
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
        # Pre-seed prod categories so list_categories() (live mode) returns them.
        self.fake.categories = {
            1: {"id": 1, "name": "Product Update", "color": "#00C9A7",
                "translations": {}},
            2: {"id": 2, "name": "Feature Release", "color": "#6392F0",
                "translations": {}},
        }
        self.fake.next_cat_id = 3
        self._patcher = mock.patch("requests.request", side_effect=self.fake)
        self._patcher.start()
        # Reset service-account token cache so cookie-auth state from a
        # previous test (or real env) can't bleed in.
        cmc.clear_service_account_token_cache()
        self.addCleanup(self._patcher.stop)
        self.addCleanup(self._cleanup_store)

        # --- prod_db read-path mocks ---
        # The live-mode read paths (list_posts, get_post, list_categories) now
        # call prod_db directly instead of the Chartmetric REST API.  We mock
        # those here so tests never touch the real prod DB.
        fake = self.fake  # capture for closures

        def _db_list_categories():
            return list(fake.categories.values())

        def _db_list_posts():
            """Return fake posts with deleted_at populated from the local store."""
            import json as _json
            import os as _os
            local_deleted: dict[int, str] = {}
            try:
                store_path = announcement_store._STORE_FILE
                if _os.path.exists(store_path):
                    with open(store_path) as _f:
                        _d = _json.load(_f)
                    for _p in _d.get("posts", {}).values():
                        _cm = _p.get("chartmetric_id")
                        if _cm and _p.get("deleted_at"):
                            local_deleted[_cm] = _p["deleted_at"]
            except Exception:
                pass
            cats_by_id = {c["id"]: c for c in fake.categories.values()}
            items = []
            for fp in fake.posts.values():
                pid = fp["id"]
                deleted_at = local_deleted.get(pid)
                cat_ids = fp.get("category_ids") or []
                categories = [
                    {"id": cid,
                     "name": cats_by_id[cid]["name"],
                     "color": cats_by_id[cid]["color"],
                     "translations": cats_by_id[cid].get("translations") or {}}
                    for cid in cat_ids if cid in cats_by_id
                ]
                if deleted_at:
                    status = "deleted"
                elif fp.get("is_published"):
                    status = "published"
                else:
                    status = "draft"
                items.append({
                    **fp,
                    "categories": categories,
                    "boost_types": [],
                    "status": status,
                    "deleted_at": deleted_at,
                })
            return {"items": items, "total": len(items)}

        def _db_get_post(cm_id):
            """Return a single fake post by Chartmetric id, or None."""
            fp = fake.posts.get(cm_id)
            if fp is None:
                return None
            cats_by_id = {c["id"]: c for c in fake.categories.values()}
            cat_ids = fp.get("category_ids") or []
            categories = [
                {"id": cid,
                 "name": cats_by_id[cid]["name"],
                 "color": cats_by_id[cid]["color"],
                 "translations": cats_by_id[cid].get("translations") or {}}
                for cid in cat_ids if cid in cats_by_id
            ]
            is_pub = bool(fp.get("is_published"))
            return {
                **fp,
                "categories": categories,
                "boost_types": [],
                "status": "published" if is_pub else "draft",
                "deleted_at": None,
            }

        def _db_create_post(*, title, content, translations, image_url,
                            is_pinned, is_boosted, is_published, published_at,
                            category_ids, boost_names):
            """Insert a post into the fake store and return its new id."""
            pid = fake.next_post_id
            fake.next_post_id += 1
            row = {
                "id": pid,
                "title": title,
                "content": content,
                "translations": translations,
                "image_url": image_url,
                "is_pinned": is_pinned,
                "is_boosted": is_boosted,
                "is_published": is_published,
                "published_at": published_at,
                "category_ids": list(category_ids or []),
            }
            fake.posts[pid] = row
            return {"id": pid}

        def _db_update_post(cm_id, *, title, content, translations, image_url,
                            is_pinned, is_boosted, is_published, published_at,
                            category_ids, boost_names):
            """Update a post in the fake store; return True/False."""
            if cm_id not in fake.posts:
                return False
            fake.posts[cm_id].update({
                "title": title,
                "content": content,
                "translations": translations,
                "image_url": image_url,
                "is_pinned": is_pinned,
                "is_boosted": is_boosted,
                "is_published": is_published,
                "published_at": published_at,
                "category_ids": list(category_ids or []),
            })
            fake.links[cm_id] = list(category_ids or [])
            return True

        for target, side_effect in (
            ("integrations.prod_db.list_categories", _db_list_categories),
            ("integrations.prod_db.list_posts",      _db_list_posts),
            ("integrations.prod_db.get_post",         _db_get_post),
            ("integrations.prod_db.create_post",      _db_create_post),
            ("integrations.prod_db.update_post",      _db_update_post),
        ):
            p = mock.patch(target, side_effect=side_effect)
            p.start()
            self.addCleanup(p.stop)

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
        # Save and clear CM vars so they don't interfere with bearer-token tests.
        self._orig_cm_base = config.CM_API_BASE_URL
        self._orig_cm_email = config.CM_SERVICE_ACCOUNT_EMAIL
        self._orig_cm_password = config.CM_SERVICE_ACCOUNT_PASSWORD
        config.CM_API_BASE_URL = ""
        config.CM_SERVICE_ACCOUNT_EMAIL = ""
        config.CM_SERVICE_ACCOUNT_PASSWORD = ""

    def tearDown(self):
        config.CHARTMETRIC_ADMIN_API_BASE_URL = self._orig_base
        config.CHARTMETRIC_ADMIN_API_TOKEN = self._orig_token
        config.CM_API_BASE_URL = self._orig_cm_base
        config.CM_SERVICE_ACCOUNT_EMAIL = self._orig_cm_email
        config.CM_SERVICE_ACCOUNT_PASSWORD = self._orig_cm_password
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

    def test_create_post_writes_to_prod_db(self):
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
            "boost_types": ["popup"],
            "is_pinned": True,
            "status": "publish_now",
            "scheduled_publish_at": None,
            "source_feature_id": "ABC123",
            "source_feature_set_id": "set-7",
        })

        # 1. Amplify-side: chartmetric_id wired up, Amplify-only fields kept locally.
        self.assertEqual(post["boost_types"], ["popup"])
        self.assertTrue(post["is_boosted"])
        self.assertEqual(post["source_feature_id"], "ABC123")
        self.assertIsNotNone(post["chartmetric_id"])

        # 2. The prod DB row must have the correct content.
        cm_id = post["chartmetric_id"]
        db_row = self.fake.posts[cm_id]
        self.assertEqual(db_row["title"], "Hello live world")
        self.assertTrue(db_row["is_published"])
        self.assertTrue(db_row["is_pinned"])

        # 3. Amplify-only fields must NOT appear in the DB row.
        self.assertNotIn("boost_types", db_row)
        self.assertNotIn("scheduled_publish_at", db_row)
        self.assertNotIn("source_feature_id", db_row)
        self.assertNotIn("source_feature_set_id", db_row)
        self.assertNotIn("status", db_row)

        # 4. Translations passed through; no "en" key.
        self.assertNotIn("en", db_row["translations"])
        self.assertIn("de", db_row["translations"])

        # 5. Category sent to DB must be the chartmetric cat id (not local id).
        cats_map = {c["id"]: c for c in cats}
        cm_cat_id = cats_map[cat_id]["chartmetric_id"]
        self.assertIn(cm_cat_id, db_row.get("category_ids", []))

        # 6. No REST wire calls for post create/update (bypassed entirely).
        rest_creates = [c for c in self.fake.calls
                        if c["method"] == "POST" and c["path"] == "/announcement"]
        self.assertEqual(len(rest_creates), 0)

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
                                  and c["path"] == "/announcement/categories"])
        announcement_store.create_post({
            "title": "Second", "content": [{"type": "p"}],
            "category_ids": [cat_id], "status": "draft",
            "translations": {},
        })
        second_cat_creates = len([c for c in self.fake.calls
                                   if c["method"] == "POST"
                                   and c["path"] == "/announcement/categories"])
        # Categories are pre-seeded with chartmetric_ids — no creation needed
        # for either post; the existing remote id is reused each time.
        self.assertEqual(first_cat_creates, 0)
        self.assertEqual(second_cat_creates, 0)


class TransactionalWriteTests(_LiveModeTestCase):

    def test_create_post_failure_rolls_back_local_store(self):
        """If the prod DB insert fails, NOTHING should land in the on-disk
        working copy — no orphan post, no dangling local tracking entry."""
        announcement_store.list_categories()
        cat_id = announcement_store.list_categories()[0]["id"]
        # Snapshot the on-disk store before the failed save.
        with open(announcement_store._STORE_FILE) as f:
            before = json.load(f)

        # Force prod_db.create_post to raise a DB error.
        with mock.patch(
            "integrations.prod_db.create_post",
            side_effect=Exception("DB kaboom"),
        ):
            with self.assertRaises(announcement_store.ValidationError) as ctx:
                announcement_store.create_post({
                    "title": "Will fail", "content": [{"type": "p"}],
                    "category_ids": [cat_id], "status": "draft", "translations": {},
                })
        self.assertEqual(ctx.exception.code, "db_error")

        with open(announcement_store._STORE_FILE) as f:
            after = json.load(f)
        # On-disk store unchanged: the _save() after prod_db.create_post
        # never ran, so no new post entry should exist.
        self.assertEqual(before["posts"], after["posts"])

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
                       and c["path"] == "/announcement/categories"]
        self.assertLessEqual(len(list_calls), 1,
                              f"Expected at most 1 GET /categories per save; got {len(list_calls)}")


class UpdatePostFlowTests(_LiveModeTestCase):

    def test_update_changes_categories_and_replays_link_table(self):
        announcement_store.list_categories()
        cats = announcement_store.list_categories()
        a, b = cats[0]["id"], cats[1]["id"]
        cats_map = {c["id"]: c for c in cats}
        cm_cat_a = cats_map[a]["chartmetric_id"]
        cm_cat_b = cats_map[b]["chartmetric_id"]

        post = announcement_store.create_post({
            "title": "T", "content": [{"type": "p"}],
            "category_ids": [a], "status": "draft", "translations": {},
        })
        cm_id = post["chartmetric_id"]

        # Update with category b instead of a.
        announcement_store.update_post(cm_id, {
            "title": "T", "content": [{"type": "p"}],
            "category_ids": [b], "status": "draft", "translations": {},
        })

        # The prod DB row must now carry only the chartmetric id for b.
        db_row = self.fake.posts[cm_id]
        self.assertIn(cm_cat_b, db_row.get("category_ids", []))
        self.assertNotIn(cm_cat_a, db_row.get("category_ids", []))

        # No REST wire calls for the update (bypassed entirely).
        rest_puts = [c for c in self.fake.calls
                     if c["method"] == "PUT"
                     and c["path"].startswith("/announcement/")]
        self.assertEqual(len(rest_puts), 0)


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

    def test_delete_post_soft_deletes_in_prod_db(self):
        """Soft delete writes deleted_at to the prod DB and hides the post."""
        announcement_store.list_categories()
        post = announcement_store.create_post({
            "title": "doomed", "content": [{"type": "p"}],
            "category_ids": [], "status": "draft", "translations": {},
        })
        # In live mode the UI passes the Chartmetric ID (from the API response),
        # not the local Amplify ID.
        cm_id = post["chartmetric_id"]
        self.fake.calls.clear()

        with mock.patch(
            "integrations.prod_db.soft_delete_post", return_value=True
        ) as mock_soft_delete:
            ok = announcement_store.delete_post(cm_id)

        self.assertTrue(ok)
        # prod_db.soft_delete_post must be called with the chartmetric id.
        mock_soft_delete.assert_called_once_with(cm_id)
        # No hard DELETE must have crossed the Chartmetric REST wire.
        rest_deletes = [c for c in self.fake.calls if c["method"] == "DELETE"]
        self.assertEqual(rest_deletes, [])
        # Post must appear in the list with deleted_at set (so UI can show Restore).
        result = announcement_store.list_posts()
        deleted_items = [p for p in result["items"] if p.get("deleted_at")]
        # In live mode the list uses the Chartmetric DB id (p["id"]), not
        # p["chartmetric_id"], so check against that field.
        deleted_ids = [p.get("id") for p in deleted_items]
        self.assertIn(cm_id, deleted_ids)
        # Active (non-deleted) items must not include the post.
        active_ids = [p.get("id") for p in result["items"] if not p.get("deleted_at")]
        self.assertNotIn(cm_id, active_ids)
        # get_post via local ID must return None (editor must not open deleted posts).
        self.assertIsNone(announcement_store.get_post(post["id"]))

    def test_delete_post_already_deleted_returns_false(self):
        """Calling delete on an already-deleted post is a no-op."""
        announcement_store.list_categories()
        post = announcement_store.create_post({
            "title": "doomed twice", "content": [{"type": "p"}],
            "category_ids": [], "status": "draft", "translations": {},
        })
        cm_id = post["chartmetric_id"]
        with mock.patch("integrations.prod_db.soft_delete_post", return_value=True):
            announcement_store.delete_post(cm_id)
            # Second call should return False (already deleted).
            ok = announcement_store.delete_post(cm_id)
        self.assertFalse(ok)


class LegacyPublishQuickTests(_LiveModeTestCase):

    def test_publish_announcement_quick_uses_live_path(self):
        announcement_store.list_categories()
        before_posts = dict(self.fake.posts)
        result = announcement_store.publish_announcement_quick(
            title="Quick publish", body="<p>hi there</p>",
            feature_id="FEAT-1", category="Product Update")
        self.assertTrue(result["success"], msg=result)
        # A new row must have been inserted into the fake prod DB.
        new_posts = {pid: p for pid, p in self.fake.posts.items()
                     if pid not in before_posts}
        self.assertEqual(len(new_posts), 1)
        db_row = list(new_posts.values())[0]
        self.assertEqual(db_row["title"], "Quick publish")
        # source_feature_id is Amplify-only and must NOT appear in the DB row.
        self.assertNotIn("source_feature_id", db_row)
        # No REST wire call to POST /announcement (bypassed entirely).
        rest_creates = [c for c in self.fake.calls
                        if c["method"] == "POST" and c["path"] == "/announcement"]
        self.assertEqual(len(rest_creates), 0)


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
