# Chartmetric Announcement Admin API Contract

This document specifies the REST contract Amplify uses to drive the live `announcement_post` / `announcement_category` / `l_announcement_post_category` tables in the Chartmetric web app.

## Operating modes

The Amplify admin always keeps a **local working copy** in `.announcement_store.json`. That working copy:

* owns its own integer id space (`post.id`, `category.id`) so the UI is stable even if the Chartmetric-side ids are renumbered;
* tracks **Amplify-only metadata** (`display_format`, `scheduled_publish_at`, `source_feature_id`, `source_feature_set_id`) that intentionally does **not** exist on the Chartmetric tables;
* persists a `chartmetric_id` on every post / category record once it has been pushed live, so subsequent updates / deletes target the correct remote row.

There are two runtime modes, decided per-request:

1. **Stub** — used whenever EITHER `CHARTMETRIC_ADMIN_API_BASE_URL` or `CHARTMETRIC_ADMIN_API_TOKEN` is missing. The working copy is the only store; nothing leaves the Amplify host.
2. **Live** — used automatically when BOTH `CHARTMETRIC_ADMIN_API_BASE_URL` and `CHARTMETRIC_ADMIN_API_TOKEN` are set. Every create / update / delete is also pushed to the Chartmetric admin REST API via the dedicated client at `integrations/chartmetric_announcement_client.py`.

> **Reads are intentionally local-only.** Even in live mode, `list_posts` / `get_post` / `list_categories` continue to serve from the local working copy; only writes (create / update / delete / boost / category sync) round-trip to Chartmetric. This is by design — the working copy is the source of truth for Amplify-only metadata (`display_format`, `scheduled_publish_at`, source-feature ids), so reading from Chartmetric would lose that context. Use the chartmetric-api admin UI directly to audit the canonical remote rows.

`ANNOUNCEMENTS_STUB_MODE` is a **kill switch only**: leave it unset for normal operation. Setting it to `1` / `true` / `yes` / `on` pins Amplify to its local working copy even when the live env vars are configured — useful for incident response.

The "Test Chartmetric connection" button in the admin UI hits `GET /api/admin/announcement-mode?ping=1`, which calls into the client's `ping()` (a `GET /admin/announcement/categories`) and surfaces the HTTP status, response body preview, and reachable category count.

---

## 1. Auth

Every request from Amplify carries:

```
Authorization: Bearer ${CHARTMETRIC_ADMIN_API_TOKEN}
Content-Type: application/json; charset=utf-8
```

The token is a long-lived service token issued by the chartmetric-api team. It SHOULD identify the calling system as `amplify-admin` for audit logs. Token scope MUST be limited to the announcement admin endpoints only.

In stub mode (no base URL / no token, or the kill switch above) Amplify reads/writes only the local JSON store at `.announcement_store.json` and never sends an Authorization header.

---

## 2. Live-side schema (already present in chartmetric-api)

Three tables back the wire contract — Amplify does **not** request any additional columns:

```sql
CREATE TABLE announcement_post (
    id              SERIAL PRIMARY KEY,
    title           TEXT NOT NULL,
    content         JSONB NOT NULL,
    translations    JSONB NOT NULL DEFAULT '{}'::jsonb,   -- see §4
    image_url       TEXT,
    is_published    BOOLEAN NOT NULL DEFAULT false,
    is_pinned       BOOLEAN NOT NULL DEFAULT false,
    is_boosted      BOOLEAN NOT NULL DEFAULT false,
    published_at    TIMESTAMP WITHOUT TIME ZONE,
    created_at      TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
    modified_at     TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE TABLE announcement_category (
    id              SERIAL PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    color           TEXT NOT NULL,
    translations    JSONB NOT NULL DEFAULT '{}'::jsonb,   -- see §4
    created_at      TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
    modified_at     TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE TABLE l_announcement_post_category (
    announcement_post_id     INT NOT NULL REFERENCES announcement_post(id) ON DELETE CASCADE,
    announcement_category_id INT NOT NULL REFERENCES announcement_category(id) ON DELETE CASCADE,
    PRIMARY KEY (announcement_post_id, announcement_category_id)
);
```

### Fields the wire payload does NOT carry

| Amplify-only field       | Lives in         | Why it isn't on the wire                                                                 |
| ------------------------ | ---------------- | ---------------------------------------------------------------------------------------- |
| `display_format`         | working copy     | Chartmetric currently renders all in-app posts the same way; format is an editorial hint that determines whether Amplify previews it as a drawer card or popup. |
| `scheduled_publish_at`   | working copy     | Scheduling is performed by an Amplify cron — it flips `is_published=true` and PUTs at the scheduled time. |
| `source_feature_id` / `source_feature_set_id` | working copy | Used by the "Pre-fill from Feature" UI; Chartmetric has no foreign-key column to honor. |

The Amplify client strips all of these before sending any POST/PUT to Chartmetric. They round-trip safely because the local working copy is always read first when the UI lists / fetches posts.

### Status mapping (Amplify-side, not on the wire)

`status` is **derived** in Amplify from the live columns plus the local `scheduled_publish_at`:

| Local state  | `is_published` | `scheduled_publish_at` (local)  |
| ------------ | -------------- | ------------------------------- |
| draft        | `false`        | `null`                          |
| scheduled    | `false`        | `>= now()`                      |
| published    | `true`         | `null`                          |

When Amplify pushes a post to Chartmetric for a `schedule` status, it sends `is_published=false` and waits — the scheduler flips `is_published` to `true` and re-PUTs the row at firing time.

---

## 3. Slate.js content shape

Both `content` (English) and every entry inside `translations[lang].content` use the same Slate.js block array. Amplify's HTML serializer emits these block types:

```jsonc
[
  { "type": "paragraph", "children": [ { "text": "Hello " }, { "text": "world", "bold": true } ] },
  { "type": "heading-one",   "children": [ { "text": "Big title" } ] },
  { "type": "heading-two",   "children": [ { "text": "Section" } ] },
  { "type": "heading-three", "children": [ { "text": "Sub-section" } ] },
  { "type": "bulleted-list", "children": [
      { "type": "list-item", "children": [ { "text": "First bullet" } ] }
  ]},
  { "type": "numbered-list", "children": [
      { "type": "list-item", "children": [ { "text": "First step" } ] }
  ]},
  { "type": "image", "url": "https://cdn.chartmetric.com/...", "alt": "Caption", "children": [ { "text": "" } ] },
  { "type": "video", "url": "https://cdn.chartmetric.com/...", "children": [ { "text": "" } ] },
  { "type": "link", "url": "https://chartmetric.com", "children": [ { "text": "Visit" } ] },
  { "type": "divider", "children": [ { "text": "" } ] }
]
```

Inline marks supported on text nodes: `bold`, `italic`, `underline`, `code`. Translation MUST translate only `text` values; `type`, `url`, `alt`, and inline marks remain unchanged.

---

## 4. Translations JSONB shape

`announcement_post.translations`:

```jsonc
{
  "de": { "title": "...", "content": [/* Slate blocks */] },
  "es": { "title": "...", "content": [...] },
  "fr": { "title": "...", "content": [...] },
  "ja": { "title": "...", "content": [...] },
  "ko": { "title": "...", "content": [...] },
  "pt": { "title": "...", "content": [...] }
}
```

`announcement_category.translations`:

```jsonc
{
  "de": { "name": "..." },
  "es": { "name": "..." },
  "fr": { "name": "..." },
  "ja": { "name": "..." },
  "ko": { "name": "..." },
  "pt": { "name": "..." }
}
```

English (`en`) is NEVER stored in `translations` — it lives in `title` / `content` / `name`. Amplify rejects any payload whose `translations` object carries an `"en"` key with a `400 validation_error`.

Per-locale entries MUST be objects with **all** required fields present and non-empty — partial blobs are rejected:

* On a post: `{ "title": <non-empty string>, "content": <non-empty Slate.js block array> }`. Both fields are required.
* On a category: `{ "name": <non-empty string> }`. Required.

Unknown locale keys (anything outside `de`, `es`, `fr`, `ja`, `ko`, `pt`) are silently dropped from the wire payload.

---

## 5. Endpoints

All endpoints below are scoped under `/admin` to keep them out of the public reader API.

### 5.1 Posts

#### `GET /admin/announcement?status=&category=&search=&offset=&limit=`

List posts (any status). Mirrors the existing public `/announcement/list` shape but always returns drafts and scheduled posts too, plus the new `display_format` and `scheduled_publish_at`, and a derived `status` field.

**Query params (all optional):**

| name       | type    | notes                                         |
| ---------- | ------- | --------------------------------------------- |
| `status`   | string  | `draft` \| `scheduled` \| `published` \| `all` (default `all`) |
| `category` | string  | category name (case-insensitive)              |
| `search`   | string  | substring match on `title`                    |
| `offset`   | int     | default `0`                                   |
| `limit`    | int     | default `25`, max `100`                       |

**200 OK** — `{ "items": [Post...], "total": <int> }`

#### `GET /admin/announcement/:id` — full post detail (same `Post` shape).

#### `POST /admin/announcement` — create a post.

Request body — `PostInput` (note: Amplify-only fields like `display_format`, `scheduled_publish_at`, `source_feature_*`, and the local `status` enum are NEVER sent — see §2):

```jsonc
{
  "title": "string, required, 1..300 chars",
  "content": [/* Slate blocks, required, non-empty */],
  "translations": { /* optional, may be {}; never contains an "en" key */ },
  "image_url": "https://... or null",
  "is_pinned": false,
  "is_boosted": false,
  "is_published": false,            // Amplify pushes the literal value
  "published_at": null,             // ISO timestamp; non-null only on publish
  "category_ids": [10, 23]          // Chartmetric category ids (see §5.1.5)
}
```

Notes:

- `is_published`, `is_pinned`, and `is_boosted` are sent as-is. Amplify is responsible for setting them according to the editorial intent (Save Draft, Publish now, Schedule).
- Scheduling is performed *by Amplify*: the working copy keeps `scheduled_publish_at` and an Amplify worker re-PUTs the post with `is_published=true`, `published_at=NOW()` when the schedule fires.
- `category_ids` are the **Chartmetric** ids — Amplify resolves its local category ids to remote ids via the auto-create flow (see §5.1.5) before sending.

**201 Created** — full `Post` row including the new `id`.

#### `PUT /admin/announcement/:id` — full update; same body as POST.

Updates always bump `modified_at=NOW()`. If the post is missing on Chartmetric, Amplify falls back to a fresh POST and re-records the new id.

**200 OK** — full `Post` row.

#### `PATCH /admin/announcement/:id/boost`

Toggle the `is_boosted` flag without touching anything else. Used by the Preview pane's **Boost as popup** switch so a marketer can flip an already-published post live without re-PUTting the entire body.

This endpoint is **only** invoked from the UI for posts whose local status is `published` AND that already have a recorded `chartmetric_id`. For drafts and scheduled posts the toggle is staged in the editor form and persisted as part of the next Save / Publish — it does **not** hit the wire. The `set_post_boost()` helper enforces the same rule server-side and returns `409 boost_not_allowed` if called on an unsynced or unpublished post.

Request body:

```json
{ "is_boosted": true }
```

**200 OK** — `{ "id": <int>, "is_boosted": true }` (or the full `Post` row).

#### `DELETE /admin/announcement/:id`

Hard-deletes the post and its `l_announcement_post_category` rows. Reactions and comments cascade per existing FK rules.

**200 OK** — `{ "deleted": true, "id": <int> }`.

#### `PUT /admin/announcement/:id/categories` — replace link-table rows

Replace **all** rows in `l_announcement_post_category` for this post. Any existing link not present in `category_ids` is removed; missing ones are inserted.

Request body:

```json
{ "category_ids": [10, 23, 41] }
```

**200 OK** — `{ "id": <int>, "category_ids": [10, 23, 41] }`.

Amplify always issues this immediately after a successful POST/PUT of a post so the link table is in sync. If a category id is unknown to Chartmetric, the server SHOULD return `404 not_found` rather than silently dropping the row.

### 5.1.5 Auto-creating categories by name

When the marketer assigns a category to a post in Amplify, the working copy holds the *local* category id. Before pushing the post Amplify resolves each local id to the corresponding Chartmetric id by:

1. checking the local record's `chartmetric_id`;
2. on cache miss, calling `GET /admin/announcement/categories` (cached for the duration of the request) and matching `name.lower()` against the local name;
3. if no match exists, calling `POST /admin/announcement/categories` with the local `{name, color, translations}` and using the freshly minted id.

This means category names are the de-facto cross-environment key. **Names are case-insensitively unique.** The chartmetric-api side MUST honor the existing `UNIQUE` constraint on `announcement_category.name` and return `409 category_name_taken` if a duplicate slips through (Amplify retries by re-listing).

### 5.2 Categories

#### `GET /admin/announcement/categories`

Returns ALL categories with a derived `posts_count`:

```jsonc
[
  { "id": 1, "name": "New Feature", "color": "#00C9A7",
    "translations": {...}, "posts_count": 12 },
  ...
]
```

#### `POST /admin/announcement/categories` — body `CategoryInput`:

```jsonc
{
  "name": "string, required, 1..80 chars, unique case-insensitive",
  "color": "#RRGGBB, required (7-char hex)",
  "translations": { /* optional, may be {} */ }
}
```

**201 Created** — `Category` object.

#### `PUT /admin/announcement/categories/:id` — same body as POST.

#### `DELETE /admin/announcement/categories/:id`

**409 Conflict** if `posts_count > 0` with `{ "error": "Category in use", "posts_count": <int> }`.

**200 OK** otherwise — `{ "deleted": true, "id": <int> }`.

### 5.3 Media upload

#### `POST /admin/announcement/media`

Multipart form upload. Single file field `file`. Optional form field `kind` (`image` | `video`, default inferred from MIME type).

Server stores the file in the chartmetric CDN/S3 bucket and returns:

```json
{ "url": "https://cdn.chartmetric.com/announcements/abc123.png",
  "kind": "image",
  "size": 53124,
  "content_type": "image/png" }
```

Constraints:
- Allowed image MIME types: `image/png`, `image/jpeg`, `image/webp`, `image/gif`
- Allowed video MIME types: `video/mp4`, `video/webm`
- Max image size: 10 MB
- Max video size: 50 MB

If `CHARTMETRIC_MEDIA_UPLOAD_URL` is set on Amplify, the proxy forwards uploads to that URL (multipart). Otherwise Amplify falls back to its local `.publish_images/` / `.publish_videos/` storage and returns a same-host URL — the UI surfaces a banner explaining the URL won't be reachable from production until media upload is wired up.

---

## 6. Object shapes (TypeScript-style for clarity)

```ts
// Wire-side shape (what Chartmetric returns on POST/PUT/GET).
type Post = {
  id: number;
  title: string;
  content: SlateBlock[];
  translations: Record<Locale, { title: string; content: SlateBlock[] }>;
  image_url: string | null;
  is_published: boolean;
  is_pinned: boolean;
  is_boosted: boolean;
  published_at: string | null;
  created_at: string;
  modified_at: string;
  // Many-to-many via l_announcement_post_category, hydrated server-side.
  categories: { id: number; name: string; color: string;
                translations: Record<Locale, { name: string }> }[];
};

type Category = {
  id: number;
  name: string;
  color: string;          // #RRGGBB
  translations: Record<Locale, { name: string }>;
  posts_count: number;    // derived
};

type Locale = 'de' | 'es' | 'fr' | 'ja' | 'ko' | 'pt';
```

---

## 7. Validation rules (Joi-style)

```js
const postInput = Joi.object({
  title: Joi.string().trim().min(1).max(300).required(),
  content: Joi.array().items(Joi.object().unknown(true)).min(1).required(),
  translations: Joi.object().pattern(
    // 'en' is rejected — English lives in title/content/name.
    Joi.string().valid('de','es','fr','ja','ko','pt'),
    // Both fields required; partial blobs are rejected.
    Joi.object({
      title: Joi.string().min(1).max(300).required(),
      content: Joi.array().items(Joi.object().unknown(true)).min(1).required(),
    }).required()
  ).default({}),
  category_ids: Joi.array().items(Joi.number().integer().positive()).default([]),
  image_url: Joi.string().uri().allow(null, ''),
  is_pinned: Joi.boolean().default(false),
  is_boosted: Joi.boolean().default(false),
  is_published: Joi.boolean().default(false),
  published_at: Joi.date().iso().allow(null),
}).unknown(false); // reject any Amplify-only field that leaks through

const categoryInput = Joi.object({
  name: Joi.string().trim().min(1).max(80).required(),
  color: Joi.string().pattern(/^#[0-9A-Fa-f]{6}$/).required(),
  translations: Joi.object().pattern(
    Joi.string().valid('de','es','fr','ja','ko','pt'),
    Joi.object({ name: Joi.string().allow('').max(80) })
  ).default({}),
});
```

---

## 8. Error responses

All errors return JSON `{ "error": "<human-readable>", "code": "<machine_code>" }` with the status code listed below.

| Code                | HTTP | Meaning                                              |
| ------------------- | ---- | ---------------------------------------------------- |
| `validation_error`  | 400  | Joi validation failed (`details` array on body too) |
| `unauthorized`      | 401  | Missing or invalid bearer token                      |
| `not_found`         | 404  | Post / category id does not exist                    |
| `category_in_use`   | 409  | DELETE category blocked by `posts_count > 0`         |
| `category_name_taken` | 409  | Unique constraint on `name` (case-insensitive)     |
| `media_too_large`   | 413  | Upload above limits in §5.3                          |
| `unsupported_media` | 415  | MIME type not in §5.3 allowlist                      |
| `internal_error`    | 500  | Anything else                                        |

---

## 9. Out of scope for this contract

- Reactions / comments / moderation (existing reader API already covers reactions and comments; admins do not moderate them in v1).
- Per-language image variants (`image_url` is shared across locales).
- Announcement-to-feature back-references in the database. The "Pre-fill from Feature" feature in Amplify only seeds the composer; it does not persist a feature_id on the announcement record in v1.
- Analytics / view counts.
