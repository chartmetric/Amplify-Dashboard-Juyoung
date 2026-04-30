# Attachment storage (Task #99)

Amplify stores six kinds of binary attachments. Historically each kind
had its own on-disk + Postgres path. Task #99 added a single shared seam
(`integrations/attachment_store.py`) that can also push every kind to
AWS S3 — durable, cheap, and unaffected by container recycles.

The local on-disk + Postgres path is still written on every upload, so a
single S3 outage never loses bytes. Reads prefer S3 when the row was
recorded as having an S3 key; otherwise they fall back to the local
path automatically.

## Kinds covered

| Kind              | Source                                           | S3 prefix          |
| ----------------- | ------------------------------------------------ | ------------------ |
| `feature-images`  | `ai/publish_store.py: save_image`                | `feature-images/`  |
| `videos`          | `ai/publish_store.py: save_video` (MP4 bytes)    | `videos/`          |
| `video-thumbs`    | `ai/publish_store.py: save_video` (poster JPEG)  | `videos/`          |
| `external-thumbs` | `integrations/video_thumb.py` (cached YouTube)   | `external-thumbs/` |
| `hosted-emails`   | `integrations/sendgrid_client.py`                | `hosted-emails/`   |
| `announcements`   | `announcements_routes.py: upload_media_endpoint` | `announcements/`   |

## Configuration

Two env knobs. The first is the master switch; the second group are the
required AWS credentials when the switch is on.

| Variable                           | Values        | Default | Notes                                       |
| ---------------------------------- | ------------- | ------- | ------------------------------------------- |
| `AMPLIFY_IMAGE_STORAGE_BACKEND`    | `local`, `s3` | `local` | Flip to `s3` to enable durable storage.     |
| `S3_Bucket_name`                   | string        | —       | Bucket must allow `s3:PutObject`.           |
| `S3_Region`                        | string        | —       | e.g. `us-east-1`.                           |
| `S3_Access_Key`                    | string        | —       | IAM key with bucket put/delete.             |
| `S3_Secret_Access_Key`             | string        | —       | Matching secret.                            |

When `AMPLIFY_IMAGE_STORAGE_BACKEND=s3` and all four secrets are set,
new uploads land in S3 and rows record the S3 key alongside the
existing on-disk metadata. Flip the env back to `local` (or unset a
secret) and uploads return to the legacy disk + Postgres path
immediately — the seam is reversible without code changes.

## Admin endpoints

Both endpoints are gated by `AMPLIFY_ADMIN_TOKEN` (header
`X-Admin-Token`, query `?admin_token=`, or JSON body `admin_token`).
When the env var is unset, the endpoints reply 503.

### `GET /api/admin/attachments/status`

Returns the current backend, which secrets are present, the per-kind
count of items still on local disk (i.e. waiting to be backfilled), and
the in-memory ring buffer of the last 25 upload attempts.

```json
{
  "backend": "s3",
  "s3_enabled": true,
  "secrets_present": {"S3_Bucket_name": true, "S3_Region": true, "S3_Access_Key": true, "S3_Secret_Access_Key": true},
  "pending": {"feature-images": 0, "videos": 3, "video-thumbs": 3, "external-thumbs": 0, "hosted-emails": 0, "announcements": 0},
  "recent": [
    {"ts": 1714512000.1, "backend": "s3", "kind": "videos", "key": "videos/abc.../video.mp4", "bytes": 4823211, "ok": true}
  ]
}
```

### `POST /api/admin/attachments/backfill?kind=<kind>&limit=<N>`

Uploads up to `limit` (default 50, max 500) items of the given kind to
S3 and records the resulting key on the local meta / DB row. Re-run
until `pending` for that kind hits zero. Pass `kind=all` to walk every
kind in one request.

```json
{"success": true, "kind": "videos", "scanned": 12, "uploaded": 12, "thumbs_uploaded": 0, "errors": 0}
```

If S3 is not currently enabled the endpoint returns 503 with
`error: "s3_disabled"` and a `secrets_present` map so you can see what
to fix.

## Dashboard panel

Open the Lab dropdown in the dashboard top bar and pick **Attachment
Storage**. The first time you open it, you'll be prompted for the
admin token (cached in `localStorage` as `amplify_admin_token`). The
panel shows backend / secret state, pending counts, a one-click
"Backfill 50" per kind, and the same recent-uploads ring buffer the API
returns.

## Reverting

Set `AMPLIFY_IMAGE_STORAGE_BACKEND=local` (or unset any S3 secret) and
restart. Existing rows that already have an S3 key keep redirecting to
S3 — there's no automatic "rip out of S3" path because that would lose
data. New uploads return to the disk + Postgres path. To stop reading
from S3 on previously-backfilled rows, also nullify the `s3_key`
columns (manual SQL). For most rollbacks just flipping the env is
enough.
