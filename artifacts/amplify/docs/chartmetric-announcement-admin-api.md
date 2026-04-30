# Chartmetric Announcement Admin API Contract

This document specifies the REST contract that the **chartmetric-api** service must implement so the Amplify "In-App Announcements" admin can create, edit, schedule, and delete announcement posts and categories without manual SQL.

The Amplify Flask backend already exposes proxy routes at `/api/announcements/*` and `/api/announcement-categories/*` that forward to these endpoints. When `ANNOUNCEMENTS_STUB_MODE=true` (default while this contract is unimplemented), Amplify uses an in-memory + JSON cache store so the UI is fully usable end-to-end. Once the chartmetric-api endpoints below are live and `ANNOUNCEMENTS_STUB_MODE=false`, the proxy forwards every request unchanged.

---

## 1. Auth

Every request from Amplify carries:

```
Authorization: Bearer ${CHARTMETRIC_ADMIN_API_TOKEN}
Content-Type: application/json; charset=utf-8
```

The token is a long-lived service token issued by the chartmetric-api team. It SHOULD identify the calling system as `amplify-admin` for audit logs. Token scope MUST be limited to the announcement admin endpoints only.

`ANNOUNCEMENTS_STUB_MODE=true` skips the token entirely — Amplify reads/writes the local JSON store at `.announcement_store.json`.

---

## 2. Schema additions (chartmetric-api migration)

Two columns must be added to `announcement_post` to support draft + scheduled publish + display format:

```sql
ALTER TABLE announcement_post
    ADD COLUMN display_format TEXT NOT NULL DEFAULT 'banner'
        CHECK (display_format IN ('banner', 'popup', 'inline')),
    ADD COLUMN scheduled_publish_at TIMESTAMPTZ NULL;

CREATE INDEX announcement_post_scheduled_idx
    ON announcement_post (scheduled_publish_at)
    WHERE scheduled_publish_at IS NOT NULL AND is_published = FALSE;
```

A scheduled-publish worker (cron / sidecar) MUST flip `is_published = TRUE`, set `published_at = NOW()`, and clear `scheduled_publish_at` once `scheduled_publish_at <= NOW()`.

The existing `is_published` column continues to mean "currently visible to end users". Posts are filed as:

| State        | `is_published` | `scheduled_publish_at`  |
| ------------ | -------------- | ----------------------- |
| draft        | `false`        | `null`                  |
| scheduled    | `false`        | `>= now()`              |
| published    | `true`         | `null`                  |

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

English (`en`) is NEVER stored in `translations` — it lives in `title` / `content` / `name`.

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

Request body — `PostInput`:

```jsonc
{
  "title": "string, required, 1..300 chars",
  "content": [/* Slate blocks, required, non-empty */],
  "translations": { /* optional, may be {} */ },
  "category_ids": [1, 2],            // required (may be empty); array of category PKs
  "image_url": "https://... or null",
  "display_format": "banner|popup|inline",   // default "banner"
  "is_pinned": false,
  "is_boosted": false,
  "status": "draft|publish_now|schedule",    // required
  "scheduled_publish_at": "2026-05-01T15:00:00Z"  // required iff status=schedule, future timestamp
}
```

Server behavior:

- `status=publish_now` → `is_published=true`, `published_at=NOW()`, `scheduled_publish_at=null`
- `status=schedule` → `is_published=false`, `scheduled_publish_at=<provided>`, `published_at=null`
- `status=draft` → `is_published=false`, `scheduled_publish_at=null`, `published_at=null`

**201 Created** — `Post` object.

#### `PUT /admin/announcement/:id` — full update; same body as POST.

If `status=publish_now` and the post is currently `draft`/`scheduled`, set `published_at=NOW()`. Updates always bump `modified_at=NOW()`.

**200 OK** — `Post` object.

#### `DELETE /admin/announcement/:id`

Hard-deletes the post and its `l_announcement_post_category` rows. Reactions and comments cascade per existing FK rules.

**200 OK** — `{ "deleted": true, "id": <int> }`.

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
type Post = {
  id: number;
  title: string;
  content: SlateBlock[];
  translations: Record<Locale, { title: string; content: SlateBlock[] }>;
  image_url: string | null;
  is_published: boolean;
  is_pinned: boolean;
  is_boosted: boolean;
  display_format: 'banner' | 'popup' | 'inline';
  scheduled_publish_at: string | null;   // ISO8601 UTC
  published_at: string | null;
  created_at: string;
  modified_at: string;
  status: 'draft' | 'scheduled' | 'published';   // derived server-side
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
    Joi.string().valid('de','es','fr','ja','ko','pt'),
    Joi.object({
      title: Joi.string().allow('').max(300),
      content: Joi.array().items(Joi.object().unknown(true)),
    })
  ).default({}),
  category_ids: Joi.array().items(Joi.number().integer().positive()).default([]),
  image_url: Joi.string().uri().allow(null, ''),
  display_format: Joi.string().valid('banner','popup','inline').default('banner'),
  is_pinned: Joi.boolean().default(false),
  is_boosted: Joi.boolean().default(false),
  status: Joi.string().valid('draft','publish_now','schedule').required(),
  scheduled_publish_at: Joi.when('status', {
    is: 'schedule',
    then: Joi.date().iso().greater('now').required(),
    otherwise: Joi.any().strip(),
  }),
});

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
