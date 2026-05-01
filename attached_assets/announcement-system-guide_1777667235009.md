# Announcement System (Internal Frill Replacement)

An internal product announcement tool replacing Frill. Currently internal-only (v1 merged).

---

## Supported Features

| Feature                    | Description                                                                                                                                                                                                                          |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **Announcement Posts**     | Rich-content posts with Slate.js blocks (paragraphs, headings, lists, images, videos, links, dividers)                                                                                                                               |
| **Categories & Filtering** | Posts tagged with colored categories; users can multi-select filter from a dropdown                                                                                                                                                  |
| **Pinned Posts**           | Posts can be pinned to the top of the list                                                                                                                                                                                           |
| **Boosted Posts**          | Posts can be marked as boosted (auto-popup support via `useAutoPopup`)                                                                                                                                                               |
| **Emoji Reactions**        | 5-reaction scale: love, thumbsup, smile, angry, thumbsdown. One reaction per user per post (upsert). Gradient bar UI with filled/outline icon toggle                                                                                 |
| **Comments**               | Users can comment on any post. Expandable/collapsible comment section with count                                                                                                                                                     |
| **Threaded Replies**       | Unlimited nesting depth. Replies indented with left border. `@mention` tag showing parent author. Collapse at 3+ replies per thread                                                                                                  |
| **Soft Delete**            | Deleting a comment with children shows "This comment was deleted" placeholder and preserves replies. Comments with no children are hard-deleted                                                                                      |
| **Pagination**             | Infinite-scroll post list with IntersectionObserver sentinel                                                                                                                                                                         |
| **Drawer + Modal UI**      | Drawer panel (480px) for the post list; Modal for full post detail view                                                                                                                                                              |
| **i18n**                   | All UI strings translated across 7 locales: en, de, es, fr, ja, ko, pt                                                                                                                                                               |
| **Content Translation**    | Post titles, content blocks, and category names translated into 6 non-English languages (de, es, fr, ja, ko, pt) stored in a JSONB `translations` column. Frontend resolves based on `i18n.language` with automatic English fallback |

---

## Database Schema

### Tables

```
announcement_post
  id              SERIAL PRIMARY KEY
  title           TEXT
  content          JSONB              -- Slate.js content blocks
  translations    JSONB DEFAULT '{}'  -- { "de": { "title": "...", "content": [...] }, "es": {...}, ... }
  image_url       TEXT
  is_published    BOOLEAN
  is_pinned       BOOLEAN
  is_boosted      BOOLEAN
  published_at    TIMESTAMPTZ
  created_at      TIMESTAMPTZ
  modified_at     TIMESTAMPTZ

announcement_category
  id              SERIAL PRIMARY KEY
  name            TEXT
  color           TEXT               -- hex color for tag UI
  translations    JSONB DEFAULT '{}'  -- { "de": { "name": "..." }, "es": {...}, ... }

l_announcement_post_category          -- many-to-many link
  announcement_post_id    INT FK → announcement_post(id)
  announcement_category_id INT FK → announcement_category(id)

announcement_reaction_type
  id              SERIAL PRIMARY KEY
  reaction_name   TEXT               -- 'love', 'thumbsup', 'smile', 'angry', 'thumbsdown'

announcement_reaction
  id              SERIAL PRIMARY KEY
  announcement_id INT FK → announcement_post(id)
  user_info       INT FK → user_info(id)
  reaction_type   INT FK → announcement_reaction_type(id)
  modified_at     TIMESTAMPTZ
  UNIQUE(announcement_id, user_info)  -- one reaction per user per post

announcement_comment
  id                  SERIAL PRIMARY KEY
  announcement_id     INT FK → announcement_post(id)
  user_info           INT FK → user_info(id)
  content             TEXT               -- NULL when soft-deleted
  parent_comment_id   INT FK → announcement_comment(id) ON DELETE SET NULL
  is_deleted          BOOLEAN DEFAULT FALSE
  created_at          TIMESTAMPTZ
  modified_at         TIMESTAMPTZ
  INDEX idx_announcement_comment_parent ON (parent_comment_id)
```

### Translations JSONB Structure

The `translations` column on both `announcement_post` and `announcement_category` stores translated content keyed by locale code. English content stays in the existing `title`/`content` columns (no `en` key in the translations blob).

**Post translations shape:**

```json
{
  "de": { "title": "German title", "content": [/* Slate.js blocks with translated text */] },
  "es": { "title": "Spanish title", "content": [...] },
  "fr": { "title": "French title", "content": [...] },
  "ja": { "title": "Japanese title", "content": [...] },
  "ko": { "title": "Korean title", "content": [...] },
  "pt": { "title": "Portuguese title", "content": [...] }
}
```

**Category translations shape:**

```json
{
  "de": { "name": "Neue Funktion" },
  "es": { "name": "Nueva Función" },
  "fr": { "name": "Nouvelle Fonctionnalité" },
  "ja": { "name": "新機能" },
  "ko": { "name": "새 기능" },
  "pt": { "name": "Novo Recurso" }
}
```

**Key design decisions:**

- Slate.js JSON block structure is fully preserved in translations — only `text` node values are translated; `type`, `url`, `alt`, and other structural fields remain unchanged
- `image_url` is the same across all languages (no per-language images)
- Product names (Chartmetric, Onesheet, etc.) and technical terms are kept untranslated
- Existing posts were backfilled using AI-generated translations (`scripts/sql/announcement-translations-backfill.sql` in `chartmetric-api`)
- Future posts will have translations auto-generated at creation time via the admin UI

---

## API Endpoints

All endpoints are under `/announcement` and require authentication (user ID from CM request token).

### Posts

| Method | Path                                              | Description                                                                                                                                                                                                                       |
| ------ | ------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `GET`  | `/announcement/list?offset=&limit=&categories[]=` | Paginated list of published posts with categories, reaction counts, user's reaction, comments, comment count, and translations. Supports multi-select category filtering via `categories[]` query param (array of category names) |
| `GET`  | `/announcement/:announcementId`                   | Single post detail (same shape as list items)                                                                                                                                                                                     |

### Categories

| Method | Path                       | Description                                    |
| ------ | -------------------------- | ---------------------------------------------- |
| `GET`  | `/announcement/categories` | All categories (id, name, color, translations) |

### Reactions

| Method   | Path                                      | Description                                |
| -------- | ----------------------------------------- | ------------------------------------------ |
| `GET`    | `/announcement/:announcementId/reactions` | Reaction counts + current user's reaction  |
| `PUT`    | `/announcement/:announcementId/reaction`  | Upsert user reaction (`{ reaction_type }`) |
| `DELETE` | `/announcement/:announcementId/reaction`  | Remove user's reaction                     |

### Comments

| Method   | Path                                                | Description                                                                                                                                           |
| -------- | --------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| `GET`    | `/announcement/:announcementId/comments`            | All comments for a post (flat list, ordered by `created_at ASC`). Soft-deleted comments return `user_name: null`, `content: null`, `is_deleted: true` |
| `POST`   | `/announcement/:announcementId/comments`            | Add comment (`{ content, parent_comment_id? }`). `parent_comment_id` is optional for replies                                                          |
| `DELETE` | `/announcement/:announcementId/comments/:commentId` | Delete comment. If comment has children → soft-delete (sets `is_deleted=true`, nulls content). If no children → hard-delete                           |

---

## Backend Structure (`chartmetric-api`)

```
src/
  Models/
    announcement.ts              -- Announcement class with static methods
    queries/
      announcement.ts            -- Raw SQL queries (POST, CATEGORY, REACTION, COMMENT)
  Routers/
    announcementRouter.ts        -- Express router for all /announcement/* endpoints
    validators/
      announcement.ts            -- Joi validation schemas
scripts/
  sql/
    announcement-translations-backfill.sql  -- SQL backfill for existing post/category translations
```

**Key patterns:**

- Read queries use `mainDBReadNode`, write queries use `mainDBWriteNode`
- Error handling via `handleDBError(next)` and `handleDBResponse(res)`
- Native pg-promise parameter binding (`$<name>::TYPE`)
- Post list query aggregates categories (with translations), reactions, comments, and comment count in a single SELECT using subqueries
- Category filtering uses `ANY($<categories>::TEXT[])` for multi-select support
- `translations` column is returned alongside existing fields in both `POST_SELECT` and `CATEGORY.GET_ALL` queries

---

## Frontend Structure (`chartmetric-web-app`)

```
components/shared/AnnouncementPanel/
  AnnouncementPanel.tsx           -- Drawer container (entry point)
  AnnouncementModal.tsx           -- Full-post modal view (uses getTranslatedPost for translated content)
  utils.ts                        -- Date formatting, reaction order, comment tree builder, translation helpers
  utils.vtest.ts                  -- Unit tests for translation helpers (12 test cases)
  hooks/
    useAnnouncementPanel.ts       -- Panel state: pagination, multi-select filtering, modal selection
    useEmojiReactions.ts          -- Reaction upsert/delete with optimistic SWR refresh
    useComments.ts                -- Comment CRUD, reply state, tree building
    useAutoPopup.ts               -- Boosted post auto-popup logic
  components/
    AnnouncementButton.tsx        -- Sidebar trigger button (bullhorn icon + notification dot)
    AnnouncementHeader.tsx        -- Panel header with CM brand icon + close button
    AnnouncementControl.tsx       -- Category filter dropdown (sticky, multi-select, translated labels)
    AnnouncementList.tsx          -- Infinite-scroll post list with IntersectionObserver
    AnnouncementCard.tsx          -- Post card (translated title, date, categories, content preview, reactions, comments)
    AnnouncementTag.tsx           -- Colored category pill (translated category name)
    AnnouncementReactions.tsx     -- 5-emoji reaction bar with gradient background
    AnnouncementComments.tsx      -- Comment section: expand/collapse toggle, comment input with reply context
    CommentThread.tsx             -- Recursive threaded comment rendering (CommentItem + CommentThread)
    AnnouncementContentRenderer.tsx -- Slate.js block → HTML renderer (paragraphs, headings, lists, images, videos, links)
    index.ts                      -- Barrel exports
```

### Types (`common/types/announcement.ts`)

- `Announcement` — full post object with categories, reactions, comments, and `translations`
- `AnnouncementTranslation` — `{ title: string; content: ContentBlock[] }` for translated post content
- `AnnouncementCategoryTranslation` — `{ name: string }` for translated category name
- `AnnouncementComment` — comment with `parent_comment_id`, `is_deleted`, nullable `user_name`/`content`
- `AnnouncementCategory` — category with `translations: Record<string, AnnouncementCategoryTranslation>`
- `AnnouncementCategoryInfo` — inline category info (name, color, translations) embedded in post objects
- `ReactionCounts`, `ReactionType`
- `ContentBlock` union — Slate.js block types (paragraph, heading, list, image, video, divider, etc.)
- `CommentTreeNode` (in utils) — extends `AnnouncementComment` with `replies: CommentTreeNode[]`

### Translation Utilities (`utils.ts`)

- `getTranslatedPost(announcement, lang)` — returns `{ title, content }` resolved for the given language. Falls back to English (`announcement.title` / `announcement.content`) when `lang === 'en'`, translation is missing, title is empty, or content array is empty
- `getTranslatedCategoryName(category, lang)` — returns the translated category name for the given language. Falls back to English (`category.name`) when `lang === 'en'`, translation is missing, or translated name is empty

**Usage in components:**

- `AnnouncementCard` and `AnnouncementModal` call `getTranslatedPost(announcement, i18n.language)` to resolve translated title and content
- `AnnouncementTag` and `AnnouncementControl` call `getTranslatedCategoryName(category, i18n.language)` to resolve translated category labels
- `i18n.language` is obtained from `useTranslation()` hook and added to `useMemo` dependency arrays so UI re-renders on language change

### API Service (`common/apiServices/announcementService.ts`)

- `useGetAnnouncements` — SWR hook for paginated list (accepts `categories?: string[]` for multi-select filtering)
- `useGetAnnouncementById` — SWR hook for single post
- `useGetAnnouncementCategories` — SWR hook for categories
- `useGetAnnouncementReactions` — SWR hook for reaction data
- `upsertAnnouncementReaction` / `deleteAnnouncementReaction` — mutation calls
- `useGetAnnouncementComments` — SWR hook for comment list
- `addAnnouncementComment` / `deleteAnnouncementComment` — mutation calls

---

## Translation Keys (`announcementPanel` namespace)

Defined in `public/locales/{en,de,es,fr,ja,ko,pt}/common.json`:

- `title`, `description`, `filter`, `allCategories`
- `pinned`, `comments`, `noComments`, `commentPlaceholder`
- `noAnnouncements`, `loadMore`, `errorMessage`
- `reactions.love`, `reactions.terrible`
- `replyButton`, `replyingTo`, `cancelReply`, `deletedComment`, `showMoreReplies`, `replyPlaceholder`

> **Note:** These are UI string translations (button labels, headers, etc.) managed via `next-i18next`. Post/category content translations are separate and stored in the `translations` JSONB column in the database.

---

## PR History

- **v1 (merged)**: Drawer/Modal UI, reactions, comments, DB tables, Frill migration
- **v2 (in review)**: Threaded comment replies, soft-delete
  - API: [`chartmetric-api#6043`](https://github.com/chartmetric/chartmetric-api/pull/6043)
  - Web: [`chartmetric-web-app#11221`](https://github.com/chartmetric/chartmetric-web-app/pull/11221)
- **v3 (in review)**: Content translation support (JSONB translations column), multi-select category filtering, backfill SQL
  - API: [`chartmetric-api#6065`](https://github.com/chartmetric/chartmetric-api/pull/6065)
  - Web: [`chartmetric-web-app#11270`](https://github.com/chartmetric/chartmetric-web-app/pull/11270)
