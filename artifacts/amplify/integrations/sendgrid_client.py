import os
import html as html_mod
import logging

logger = logging.getLogger(__name__)


def _esc(text: str) -> str:
    return html_mod.escape(text, quote=True)


def _inline_markdown(text: str) -> str:
    import re
    placeholders = {}
    counter = [0]
    def _stash(html_value):
        key = f'\x00P{counter[0]}\x00'
        counter[0] += 1
        placeholders[key] = html_value
        return key
    def _stash_link(m):
        link_text = _esc(m.group(1))
        url = m.group(2)
        if re.match(r'^https?://', url, re.IGNORECASE) or url.startswith('mailto:'):
            return _stash(f'<a href="{_esc(url)}" target="_blank" rel="noopener noreferrer" style="color:#00C9A7;text-decoration:underline;">{link_text}</a>')
        return m.group(1)
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', _stash_link, text)
    text = re.sub(
        r'`([^`\n]+)`',
        lambda m: _stash(f'<code style="font-family:Menlo,Consolas,monospace;font-size:13px;background:#f3f4f6;color:#1a1d23;padding:2px 6px;border-radius:4px;border:1px solid #e5e7eb;">{_esc(m.group(1))}</code>'),
        text,
    )
    safe = _esc(text)
    safe = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', safe)
    safe = re.sub(r'\*(.+?)\*', r'<em>\1</em>', safe)
    for key, val in placeholders.items():
        safe = safe.replace(key, val)
    return safe


_MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


def _current_month_year() -> str:
    from datetime import datetime
    now = datetime.now()
    return f"{_MONTH_NAMES[now.month - 1]} {now.year}"


def _current_year() -> str:
    from datetime import datetime
    return str(datetime.now().year)


def _render_callout_html(text: str) -> str:
    return (
        '<div style="margin:16px 0;padding:14px 18px;background:#f0fbf8;'
        'border-left:4px solid #00C9A7;border-radius:6px;color:#1a1d23;'
        'font-size:14px;line-height:1.55;">'
        f'{_inline_markdown(text)}'
        '</div>'
    )


def _render_chip_html(label: str) -> str:
    label_clean = (label or "").strip()
    if not label_clean:
        return ''
    bg = '#f0fbf8'
    color = '#008f76'
    border = '#b8ebde'
    lower = label_clean.lower()
    if 'coming' in lower or 'soon' in lower:
        bg = '#fff7ed'
        color = '#c2410c'
        border = '#fed7aa'
    elif 'improvement' in lower:
        bg = '#eff6ff'
        color = '#1d4ed8'
        border = '#bfdbfe'
    elif 'bug' in lower or 'fix' in lower:
        bg = '#fef2f2'
        color = '#b91c1c'
        border = '#fecaca'
    elif 'mobile' in lower:
        bg = '#faf5ff'
        color = '#7e22ce'
        border = '#e9d5ff'
    elif 'infrastructure' in lower or 'platform' in lower:
        bg = '#f3f4f6'
        color = '#374151'
        border = '#d1d5db'
    elif 'deprecation' in lower or 'sunset' in lower:
        bg = '#fff7ed'
        color = '#9a3412'
        border = '#fdba74'
    return (
        '<div style="margin:18px 0 8px 0;">'
        f'<span style="display:inline-block;background:{bg};color:{color};'
        f'border:1px solid {border};border-radius:999px;padding:3px 10px;'
        f'font-size:11px;font-weight:700;letter-spacing:0.4px;text-transform:uppercase;">'
        f'{_esc(label_clean)}'
        '</span></div>'
    )


def _get_base_url() -> str:
    """Return the public base URL for embedding in outgoing emails / video links.

    Resolution order:
      1. The current Flask request's host (when in a request context). This is
         the only source that always matches whatever host the recipient just
         hit, so it's correct for both production deploys and dev previews
         even when env vars carry stale values from a different repl.
      2. ``REPLIT_DEPLOYMENT_URL`` if explicitly set (custom override).
      3. ``REPLIT_DOMAINS`` (the standard Replit env var, populated in both
         dev and production deployments — production points at the
         ``.replit.app`` URL, dev points at the ``.riker.replit.dev`` URL).
      4. ``REPLIT_DEV_DOMAIN`` (legacy fallback).
      5. ``http://localhost:5000``.

    Falling back to a stale dev-domain env var in production is what caused
    a long-running bug where every video thumbnail in a sent email pointed
    at the dev container (unreachable from a recipient's inbox), which
    rendered as broken images and "not attached" markers.
    """
    try:
        from flask import has_request_context, request as _flask_request
        if has_request_context():
            host_url = (_flask_request.host_url or "").rstrip("/")
            if host_url and not host_url.startswith("http://localhost"):
                return host_url
    except Exception:
        pass
    deploy_url = os.environ.get("REPLIT_DEPLOYMENT_URL", "")
    if deploy_url:
        return deploy_url.rstrip("/")
    domains = os.environ.get("REPLIT_DOMAINS", "")
    if domains:
        first = domains.split(",")[0].strip()
        if first:
            return f"https://{first}"
    dev_domain = os.environ.get("REPLIT_DEV_DOMAIN", "")
    if dev_domain:
        return f"https://{dev_domain}"
    return "http://localhost:5000"


# Hosted-email store: when an email is sent (or previewed), we persist the
# rendered HTML under a random token so the recipient's "View in browser"
# link can fetch it from /email/view/<token>. Tokens are unguessable
# (secrets.token_urlsafe(16) ~> 22 chars of base64url) so we don't need
# auth for the read endpoint.
HOSTED_EMAILS_DIR = os.path.realpath(
    os.path.join(os.path.dirname(__file__), "..", ".hosted_emails")
)


def _save_hosted_email(token: str, html: str) -> bool:
    """Persist `html` so /email/view/<token> can serve it later.

    Best effort: a write failure logs but does not raise, so a transient
    disk problem can't block an outbound email send.
    """
    if not token or not html:
        return False
    try:
        os.makedirs(HOSTED_EMAILS_DIR, exist_ok=True)
        # Token is base64url so it's already filesystem-safe, but we still
        # strip path separators defensively.
        safe = "".join(c for c in token if c.isalnum() or c in "-_")
        if not safe:
            return False
        path = os.path.join(HOSTED_EMAILS_DIR, f"{safe}.html")
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        return True
    except Exception as e:
        # Tokens are effectively bearer secrets (anyone with the URL can
        # read the hosted email), so log only a short prefix instead of
        # the full value.
        logger.warning(f"[hosted_email] save failed for token=<{token[:4]}...>: {e}")
        return False


def load_hosted_email(token: str) -> str | None:
    """Return the stored HTML for `token` or None if missing/invalid."""
    if not token:
        return None
    safe = "".join(c for c in token if c.isalnum() or c in "-_")
    if not safe:
        return None
    path = os.path.join(HOSTED_EMAILS_DIR, f"{safe}.html")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return None
    except Exception as e:
        # See note in _save_hosted_email — never log the full token.
        logger.warning(f"[hosted_email] load failed for token=<{token[:4]}...>: {e}")
        return None


def _build_view_in_browser_url(token: str) -> str:
    return f"{_get_base_url()}/email/view/{token}"


# Sentinel substituted per-recipient inside _send_via_resend so each recipient's
# email has a personal, signed unsubscribe URL while we only render the body
# HTML once per send batch.
UNSUBSCRIBE_PLACEHOLDER = "{{AMPLIFY_UNSUBSCRIBE_URL}}"

# CAN-SPAM-friendly fallback used when an email is sent outside an audience
# (custom typed-in recipient lists, test sends to ourselves) so there is no
# topic-subscription row to flip. Recipients can still opt out by replying
# to this address; the inbox is monitored manually. Also used as the
# List-Unsubscribe header value for those sends so Gmail/Outlook surface a
# native unsubscribe button.
GENERIC_UNSUBSCRIBE_MAILTO = "mailto:unsubscribe@chartmetric.com"


_UNSAFE_SESSION_SECRETS = {"", "amplify-dev-secret", "change-me", "dev", "secret"}


def _unsubscribe_signing_key() -> bytes | None:
    """Server-side HMAC key for unsubscribe tokens.

    Reuses SESSION_SECRET so we don't introduce a separate env var. If it's
    ever rotated, outstanding unsubscribe links become invalid (they verify
    as tampered and the page shows the friendly error).

    Returns ``None`` when SESSION_SECRET is missing or set to a known
    insecure default. Callers that mint or verify tokens fail closed in
    that case (no link rendered, all submitted tokens reject) so we
    cannot ship forgeable unsubscribe URLs to recipients.
    """
    secret = (os.environ.get("SESSION_SECRET") or "").strip()
    if secret.lower() in _UNSAFE_SESSION_SECRETS:
        return None
    return secret.encode("utf-8")


def _b64url_encode(raw: bytes) -> str:
    import base64
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    import base64
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode((s + pad).encode("ascii"))


def make_unsubscribe_token(audience_id: str, email: str, topic_id: str = "") -> str:
    """Sign (audience_id, email, topic_id, issued_at) so a recipient cannot
    tamper with the unsubscribe URL or unsubscribe someone else.

    Token format: base64url(payload_json) + "." + base64url(hmac_sha256).
    No expiry is enforced — recipients sometimes open emails months later.

    The ``topic_id`` field was added when we moved from audience-wide
    unsubscribe (Task #73) to per-topic opt-out (Task #77). Tokens minted
    before that change had no ``tp`` field; ``verify_unsubscribe_token``
    returns those with an empty ``topic_id`` and the route falls back to
    the legacy audience-wide unsubscribe path so old links still work.

    Returns an empty string when SESSION_SECRET is missing or set to a
    known insecure default; callers should treat that as "skip the link".
    """
    import hmac, hashlib, json, time as _t
    key = _unsubscribe_signing_key()
    if not key:
        logger.error("[unsubscribe] SESSION_SECRET is missing or set to an insecure default; refusing to mint unsubscribe token")
        return ""
    payload = {
        "a": (audience_id or "").strip(),
        "e": (email or "").strip().lower(),
        "t": int(_t.time()),
        "tp": (topic_id or "").strip(),
    }
    payload_b = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    sig = hmac.new(key, payload_b, hashlib.sha256).digest()
    return f"{_b64url_encode(payload_b)}.{_b64url_encode(sig)}"


def verify_unsubscribe_token(token: str) -> dict | None:
    """Return ``{"audience_id", "email", "topic_id", "issued_at"}`` if the
    token is valid and untampered, else ``None``.

    Tokens minted before per-topic unsubscribe (Task #77) have no ``tp``
    field; this returns ``topic_id=""`` for those so the route can fall
    back to the legacy audience-wide unsubscribe path. Newer tokens
    always carry ``tp``.

    Returns ``None`` when SESSION_SECRET is missing or insecure, so any
    forged or replayed token is rejected (fail closed).
    """
    import hmac, hashlib, json
    if not token or "." not in token:
        return None
    key = _unsubscribe_signing_key()
    if not key:
        logger.error("[unsubscribe] SESSION_SECRET is missing or set to an insecure default; rejecting all tokens")
        return None
    try:
        payload_part, sig_part = token.split(".", 1)
        payload_b = _b64url_decode(payload_part)
        sig = _b64url_decode(sig_part)
        expected = hmac.new(key, payload_b, hashlib.sha256).digest()
        if not hmac.compare_digest(sig, expected):
            return None
        payload = json.loads(payload_b.decode("utf-8"))
    except Exception:
        return None
    email = (payload.get("e") or "").strip().lower()
    audience_id = (payload.get("a") or "").strip()
    topic_id = (payload.get("tp") or "").strip()
    if not email:
        return None
    return {
        "audience_id": audience_id,
        "email": email,
        "topic_id": topic_id,
        "issued_at": int(payload.get("t") or 0),
    }


def build_unsubscribe_url(audience_id: str, email: str, topic_id: str = "") -> str:
    """Return the full hosted unsubscribe URL for a recipient, or an empty
    string when no token can be safely minted (insecure SESSION_SECRET)."""
    token = make_unsubscribe_token(audience_id, email, topic_id=topic_id)
    if not token:
        return ""
    return f"{_get_base_url()}/email/unsubscribe?token={token}"


def _render_footer_links_html(view_url: str = None, unsubscribe_placeholder: str = None) -> str:
    """Render the "View in browser · Privacy Policy [· Unsubscribe]" footer line.

    When ``view_url`` is provided, "View in browser" links to that hosted
    HTML page (the same content the recipient just opened, served from
    /email/view/<token>). When it's None, we fall back to the marketing
    homepage rather than emitting a dead link.

    When ``unsubscribe_placeholder`` is provided (typically
    :data:`UNSUBSCRIBE_PLACEHOLDER`), an "Unsubscribe" link is appended
    pointing at that placeholder string. The send loop substitutes either
    a personal, signed audience unsubscribe URL (audience sends) or
    :data:`GENERIC_UNSUBSCRIBE_MAILTO` (custom typed-in recipients and
    test sends) before delivery, so every email ships with a working
    unsubscribe link.
    """
    safe_view = _esc(view_url) if view_url else "https://chartmetric.com"
    unsub_html = ""
    if unsubscribe_placeholder:
        # The placeholder is substituted as-is per recipient before send;
        # don't HTML-escape it or the "{{...}}" braces would survive into
        # the final HTML.
        unsub_html = (
            '&nbsp;&middot;&nbsp;'
            f'<a href="{unsubscribe_placeholder}" class="amplify-footer-link" target="_blank" rel="noopener noreferrer" '
            'style="color:#999999;text-decoration:underline;">Unsubscribe</a>'
        )
    return (
        '<p class="amplify-footer-meta" style="margin:0 0 6px 0;color:#999999;font-size:12px;line-height:1.6;">'
        f'<a href="{safe_view}" class="amplify-footer-link" target="_blank" rel="noopener noreferrer" '
        'style="color:#999999;text-decoration:underline;">View in browser</a>'
        '&nbsp;&middot;&nbsp;'
        '<a href="https://chartmetric.com/privacy-policy" class="amplify-footer-link" target="_blank" '
        'rel="noopener noreferrer" style="color:#999999;text-decoration:underline;">Privacy Policy</a>'
        f'{unsub_html}'
        '</p>'
    )


class MediaResolutionError(Exception):
    """Raised when an `[image: ...]` or `[video: ...]` marker in an email
    body cannot be resolved to a real hosted asset at send time.

    The send must be blocked instead of shipping a broken `<img>` or video
    placeholder. The frontend uses ``missing_images`` / ``missing_videos``
    to surface an editor-visible error pointing at the offending names.
    """

    def __init__(self, missing_images: list, missing_videos: list):
        self.missing_images = list(missing_images or [])
        self.missing_videos = list(missing_videos or [])
        parts = []
        if self.missing_images:
            parts.append(
                "image(s): " + ", ".join(repr(x) for x in self.missing_images)
            )
        if self.missing_videos:
            parts.append(
                "video(s): " + ", ".join(repr(x) for x in self.missing_videos)
            )
        msg = "Cannot send email — unresolved media marker(s): " + "; ".join(parts)
        super().__init__(msg)


# ---------------------------------------------------------------------------
# Image storage seam
# ---------------------------------------------------------------------------
# Everything below the line "store these decoded image bytes and give me back
# a public URL" funnels through ``_store_image_and_get_url``. Both the send
# path (``send_email`` -> ``_build_hosted_image_map``) and the preview path
# (``/api/publish/email/preview``) call into the same seam so the rendered
# HTML never embeds base64 blobs.
#
# The active backend is selected by the ``AMPLIFY_IMAGE_STORAGE_BACKEND``
# environment variable. Two backends are implemented:
#   - ``local`` (default): Postgres with an on-disk fallback, both fronted
#     by ``/api/publish/image/hosted/<id>`` served from this app.
#   - ``s3``: AWS S3 bucket; recipients fetch directly from S3 over HTTPS.
# When ``s3`` is selected and a single upload fails, the seam falls back
# to ``local`` for that one image so the email still ships.
# Adding another object store (R2, GCS, etc.) means: implement a new
# ``_store_image_<backend>`` function, dispatch to it from
# ``_store_image_and_get_url``, and flip the env var. Callers do not change.
# ---------------------------------------------------------------------------


def _get_image_storage_backend() -> str:
    """Return the active image-storage backend name.

    Supported values: ``"local"`` (default) and ``"s3"``. Unknown values
    log an error and fall back to ``"local"``.
    """
    return (os.environ.get("AMPLIFY_IMAGE_STORAGE_BACKEND") or "local").strip().lower()


def _store_image_local(raw: bytes, ext: str, name: str) -> str | None:
    """Persist an image via the local backend (Postgres -> disk fallback).

    Returns the public URL (``/api/publish/image/hosted/<id>``) on success,
    or ``None`` if every store failed. The id format must stay hex-only so
    the serving route's path-sanitizer continues to accept it.
    """
    import uuid as _uuid
    import base64 as _b64
    import json as _json

    img_id = _uuid.uuid4().hex[:12]
    base_url = _get_base_url()

    try:
        from app import save_hosted_image_db as _save_db  # type: ignore
    except Exception as _e:
        logger.warning(f"[hosted-images] Could not import save_hosted_image_db: {_e}")
        _save_db = None

    stored_in_db = False
    db_err = None
    if _save_db is not None:
        try:
            stored_in_db = bool(_save_db(img_id, ext, str(name)[:200], raw))
        except Exception as e:
            db_err = repr(e)
            logger.warning(f"[hosted-images] DB save failed for '{name!r}': {db_err}")

    if stored_in_db:
        logger.info(
            f"[hosted-images] '{name!r}' -> DB OK (img_id={img_id}, "
            f"size={len(raw)} bytes, ext={ext})"
        )
        return f"{base_url}/api/publish/image/hosted/{img_id}"

    # Disk fallback (legacy / local dev / DB unavailable). The serving
    # route reads back a data URL from image.dat, so reconstruct one
    # here from the decoded bytes.
    try:
        from ai.publish_store import IMAGES_DIR
        img_dir = os.path.join(IMAGES_DIR, f"_hosted_{img_id}")
        os.makedirs(img_dir, exist_ok=True)
        data_url = f"data:image/{ext};base64,{_b64.b64encode(raw).decode('ascii')}"
        with open(os.path.join(img_dir, "image.dat"), "w") as f:
            f.write(data_url)
        with open(os.path.join(img_dir, "meta.json"), "w") as f:
            _json.dump({"name": str(name)[:200], "ext": ext, "id": img_id}, f)
        logger.warning(
            f"[hosted-images] '{name!r}' -> DISK ONLY (img_id={img_id}, "
            f"size={len(raw)} bytes, ext={ext}, db_err={db_err}). "
            "This will break on next redeploy."
        )
        return f"{base_url}/api/publish/image/hosted/{img_id}"
    except Exception as e:
        logger.error(
            f"[hosted-images] Disk fallback failed for '{name!r}' (img_id={img_id}): {e}"
        )
        return None


_MIME_BY_EXT = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "svg": "image/svg+xml",
}


def _store_image_s3(raw: bytes, ext: str, name: str) -> str | None:
    """Persist an email-hosted image to AWS S3 via the shared seam.

    Delegates to ``integrations.attachment_store.put`` so every attachment
    type (hosted images, feature images, videos, announcements, ...) flows
    through the same upload/log/fallback machinery. Returns the public URL
    on success or ``None`` if S3 is disabled/failed (caller falls back to
    local).
    """
    from integrations import attachment_store as _astore

    safe_ext = (ext or "png").lower().lstrip(".")
    if not safe_ext.isalnum():
        safe_ext = "png"
    content_type = _MIME_BY_EXT.get(safe_ext, f"image/{safe_ext}")
    result = _astore.put(
        kind="hosted-emails",
        key_hint=f"{str(name)[:80]}.{safe_ext}",
        raw_bytes=raw,
        content_type=content_type,
    )
    if result.get("backend") == "s3" and result.get("url"):
        return result["url"]
    return None


def _store_image_and_get_url(raw: bytes, ext: str, name: str) -> str | None:
    """Storage seam: persist a decoded image and return its public URL.

    Dispatches to the backend named by ``AMPLIFY_IMAGE_STORAGE_BACKEND``.
    Returns ``None`` if the backend could not persist the image; callers
    must treat that as a hard failure (skip the image rather than ship a
    base64 blob).
    """
    backend = _get_image_storage_backend()
    if backend == "local":
        return _store_image_local(raw, ext, name)
    if backend == "s3":
        url = _store_image_s3(raw, ext, name)
        if url is not None:
            return url
        logger.warning(
            "[attachments] kind=hosted-emails backend=s3 failed; "
            "falling back to local for this image"
        )
        return _store_image_local(raw, ext, name)
    logger.error(
        f"[attachments] Unknown AMPLIFY_IMAGE_STORAGE_BACKEND={backend!r}; "
        "falling back to local"
    )
    return _store_image_local(raw, ext, name)


def _build_hosted_image_map(images: dict) -> dict:
    """Convert an ``{name: data_url}`` map to ``{name: public_url}``.

    Each ``data:image/...;base64,...`` value is decoded once and handed to
    the storage seam (:func:`_store_image_and_get_url`) which returns a
    stable URL. ``http(s)://`` values pass through untouched. Anything we
    cannot convert is dropped from the result so the renderer reports the
    image as missing instead of inlining base64 into the email.
    """
    if not images:
        logger.info("[hosted-images] _build_hosted_image_map called with no images")
        return {}
    import re as _re
    import base64 as _b64

    backend = _get_image_storage_backend()
    logger.info(
        f"[hosted-images] _build_hosted_image_map start: count={len(images)} "
        f"backend={backend!r} keys={[repr(k) for k in list(images.keys())[:5]]}"
    )

    hosted: dict = {}
    for img_name, data_url in images.items():
        if not data_url:
            logger.warning(f"[hosted-images] Skipping '{img_name!r}': empty data_url")
            continue
        if data_url.startswith("http"):
            logger.info(
                f"[hosted-images] Pass-through http URL for '{img_name!r}': "
                f"{data_url[:120]}"
            )
            hosted[img_name] = data_url
            continue
        if data_url.startswith("data:image/"):
            m = _re.match(r"data:image/(\w+);base64,(.+)", data_url)
            if not m:
                logger.warning(
                    f"[hosted-images] Skipping '{img_name!r}': data URL did not match "
                    f"data:image/<ext>;base64,<...> (head={data_url[:60]!r})"
                )
                continue
            ext = m.group(1)
            try:
                raw = _b64.b64decode(m.group(2))
            except Exception:
                logger.warning(
                    f"[hosted-images] Skipping '{img_name!r}': invalid base64 data"
                )
                continue
            url = _store_image_and_get_url(raw, ext, str(img_name))
            if url:
                hosted[img_name] = url
            else:
                logger.error(
                    f"[hosted-images] Storage seam returned None for '{img_name!r}'; "
                    "dropping rather than inlining base64"
                )
            continue
        logger.warning(
            f"[hosted-images] Unknown data_url scheme for '{img_name!r}' "
            f"(head={data_url[:60]!r}); passing through as-is"
        )
        hosted[img_name] = data_url

    logger.info(
        f"[hosted-images] _build_hosted_image_map done: "
        f"in={len(images)} out={len(hosted)} "
        f"sample={list((k, v[:120] if isinstance(v, str) else v) for k, v in list(hosted.items())[:3])}"
    )
    return hosted


# ---------------------------------------------------------------------------
# Direct-S3 URL rewrite for rendered email HTML (Task #115)
# ---------------------------------------------------------------------------
# After ``render_email_html`` builds the final HTML string, we walk every
# ``<img src=...>`` and ``<source src=...>`` URL and rewrite anything that
# would otherwise depend on the Replit app being up (``/api/publish/image/
# hosted/<id>``, ``/api/publish/image/serve/<feature_id>``, ``/api/videos/
# <id>``, ``/api/videos/<id>/thumb``, ``/api/videos/external-thumb/<key>``)
# into the corresponding **direct, long-lived public S3 URL**.
#
# This makes downloaded ``.html`` files self-contained: every `<img src>`
# in `View Source` is an ``https://<bucket>.s3.<region>.amazonaws.com/...``
# URL that keeps working forever, even if the Replit app is offline.
#
# When a row already has the right ``s3_key`` recorded we just swap in the
# public URL. When it doesn't (older content saved before S3 was enabled),
# we upload the bytes on the fly through ``attachment_store.put`` and
# persist the returned key onto the row so future renders skip the upload.
#
# Every per-asset failure is best-effort: we log loudly and leave that one
# URL unchanged. The whole rewrite is also a no-op when ``s3_enabled()``
# is False (local-only backend) so previews stay working without S3.
# ---------------------------------------------------------------------------


def _public_s3_url_for(key: str) -> str | None:
    """Wrapper around :func:`attachment_store.s3_public_url` that warns when
    the bucket isn't configured to serve the long-lived virtual-hosted form.

    The downloaded HTML must keep working with no expiry, so we deliberately
    do NOT fall back to a presigned URL here — that would put a TTL on the
    file and break it the moment that TTL passes.
    """
    try:
        from integrations import attachment_store as _astore
        url = _astore.s3_public_url(key)
        if not url:
            logger.warning(
                f"[rewrite-s3] s3_public_url returned None for key={key!r}; "
                "bucket/region not set — leaving URL unchanged"
            )
        return url
    except Exception as e:
        logger.warning(f"[rewrite-s3] s3_public_url failed for key={key!r}: {e}")
        return None


def _try_upload_to_s3(kind: str, key_hint: str, raw: bytes, content_type: str) -> tuple:
    """Push bytes through the attachment seam. Returns ``(s3_key, s3_url)``
    or ``(None, None)`` on failure. Never raises."""
    try:
        from integrations import attachment_store as _astore
        result = _astore.put(
            kind=kind, key_hint=key_hint, raw_bytes=raw, content_type=content_type
        )
        if result.get("backend") == "s3" and result.get("key"):
            return result["key"], result.get("url")
        if result.get("error"):
            logger.warning(
                f"[rewrite-s3] kind={kind} on-the-fly upload skipped: "
                f"{result.get('error')}"
            )
    except Exception as e:
        logger.warning(f"[rewrite-s3] kind={kind} on-the-fly upload failed: {e}")
    return None, None


def _resolve_hosted_image_to_s3_url(img_id: str) -> str | None:
    """Return a direct S3 URL for a ``/api/publish/image/hosted/<img_id>`` URL,
    uploading on the fly if the row has no ``s3_key`` yet."""
    try:
        from app import _load_hosted_image_s3_meta as _meta, _load_hosted_image_db as _bytes_, \
            _set_hosted_image_s3 as _persist
    except Exception as e:
        logger.warning(f"[rewrite-s3] hosted-emails: app helpers unavailable: {e}")
        return None
    try:
        s3_meta = _meta(img_id)
    except Exception:
        s3_meta = None
    if s3_meta and s3_meta[0]:
        return _public_s3_url_for(s3_meta[0])
    db_hit = None
    try:
        db_hit = _bytes_(img_id)
    except Exception as e:
        logger.warning(f"[rewrite-s3] hosted-emails: load bytes failed for {img_id}: {e}")
    if not db_hit:
        return None
    ext, raw = db_hit
    safe_ext = (ext or "png").lower().lstrip(".")
    if not safe_ext.isalnum():
        safe_ext = "png"
    content_type = _MIME_BY_EXT.get(safe_ext, f"image/{safe_ext}")
    s3_key, s3_url = _try_upload_to_s3(
        kind="hosted-emails",
        key_hint=f"{img_id}.{safe_ext}",
        raw=raw,
        content_type=content_type,
    )
    if not s3_key:
        return None
    try:
        _persist(img_id, s3_key, s3_url or "")
    except Exception as e:
        logger.warning(f"[rewrite-s3] hosted-emails: persist s3_key failed for {img_id}: {e}")
    return _public_s3_url_for(s3_key)


def _resolve_feature_image_to_s3_url(feature_id: str) -> str | None:
    """Return a direct S3 URL for a ``/api/publish/image/serve/<feature_id>``
    URL, uploading the stored ``dataUrl`` to S3 on the fly if needed."""
    try:
        from ai.publish_store import get_image as _get, set_publish_image_s3 as _persist
    except Exception as e:
        logger.warning(f"[rewrite-s3] feature-images: store helpers unavailable: {e}")
        return None
    try:
        img = _get(feature_id)
    except Exception as e:
        logger.warning(f"[rewrite-s3] feature-images: get failed for {feature_id}: {e}")
        return None
    if not img:
        return None
    s3_key = (img.get("s3_key") or "").strip()
    if s3_key:
        return _public_s3_url_for(s3_key)
    data_url = img.get("dataUrl") or ""
    if not data_url.startswith("data:image/"):
        return None
    import re as _re
    import base64 as _b64
    m = _re.match(r"data:image/(\w+);base64,(.+)", data_url)
    if not m:
        return None
    ext = m.group(1).lower()
    try:
        raw = _b64.b64decode(m.group(2))
    except Exception:
        return None
    is_gif = bool(img.get("is_gif"))
    content_type = "image/gif" if (is_gif or ext == "gif") else f"image/{ext}"
    new_key, new_url = _try_upload_to_s3(
        kind="feature-images",
        key_hint=f"{feature_id}.{ext}",
        raw=raw,
        content_type=content_type,
    )
    if not new_key:
        return None
    try:
        _persist(feature_id, new_key, new_url or "", content_type)
    except Exception as e:
        logger.warning(f"[rewrite-s3] feature-images: persist failed for {feature_id}: {e}")
    return _public_s3_url_for(new_key)


def _resolve_video_thumb_to_s3_url(video_id: str) -> str | None:
    """Return a direct S3 URL for a ``/api/videos/<video_id>/thumb`` URL,
    uploading the cached thumbnail JPEG on the fly if needed."""
    try:
        from ai.publish_store import (
            get_video_meta as _meta,
            get_video_thumb_path as _thumb_path,
            set_video_s3_keys as _persist,
        )
    except Exception as e:
        logger.warning(f"[rewrite-s3] video-thumbs: store helpers unavailable: {e}")
        return None
    try:
        meta = _meta(video_id) or {}
    except Exception as e:
        logger.warning(f"[rewrite-s3] video-thumbs: meta failed for {video_id}: {e}")
        meta = {}
    s3_thumb_key = (meta.get("s3_thumb_key") or "").strip()
    if s3_thumb_key:
        return _public_s3_url_for(s3_thumb_key)
    try:
        thumb_path = _thumb_path(video_id)
    except Exception:
        thumb_path = None
    if not thumb_path:
        return None
    try:
        with open(thumb_path, "rb") as f:
            raw = f.read()
    except Exception as e:
        logger.warning(f"[rewrite-s3] video-thumbs: read thumb failed for {video_id}: {e}")
        return None
    new_key, new_url = _try_upload_to_s3(
        kind="video-thumbs",
        key_hint=f"videos/{video_id}/thumb.jpg",
        raw=raw,
        content_type="image/jpeg",
    )
    if not new_key:
        return None
    try:
        _persist(video_id, s3_thumb_key=new_key, s3_thumb_url=new_url or "")
    except Exception as e:
        logger.warning(f"[rewrite-s3] video-thumbs: persist failed for {video_id}: {e}")
    return _public_s3_url_for(new_key)


def _resolve_video_body_to_s3_url(video_id: str) -> str | None:
    """Return a direct S3 URL for a ``/api/videos/<video_id>`` URL (the
    video body — used in ``<source src>`` tags). Uploads the file on the
    fly if no ``s3_key`` is recorded yet."""
    try:
        from ai.publish_store import (
            get_video_meta as _meta,
            get_video_path as _path,
            set_video_s3_keys as _persist,
        )
    except Exception as e:
        logger.warning(f"[rewrite-s3] videos: store helpers unavailable: {e}")
        return None
    try:
        meta = _meta(video_id) or {}
    except Exception:
        meta = {}
    s3_key = (meta.get("s3_key") or "").strip()
    if s3_key:
        return _public_s3_url_for(s3_key)
    try:
        result = _path(video_id)
    except Exception:
        return None
    if not result or not result[0]:
        return None
    video_path, vmeta = result
    ext = ((vmeta or {}).get("ext", ".mp4") or ".mp4").lower()
    if not ext.startswith("."):
        ext = "." + ext
    try:
        with open(video_path, "rb") as f:
            raw = f.read()
    except Exception as e:
        logger.warning(f"[rewrite-s3] videos: read body failed for {video_id}: {e}")
        return None
    content_type = {
        ".mp4": "video/mp4", ".mov": "video/quicktime",
        ".webm": "video/webm", ".avi": "video/x-msvideo",
        ".mkv": "video/x-matroska",
    }.get(ext, "video/mp4")
    new_key, new_url = _try_upload_to_s3(
        kind="videos",
        key_hint=f"videos/{video_id}/video{ext}",
        raw=raw,
        content_type=content_type,
    )
    if not new_key:
        return None
    try:
        _persist(video_id, s3_key=new_key, s3_url=new_url or "")
    except Exception as e:
        logger.warning(f"[rewrite-s3] videos: persist failed for {video_id}: {e}")
    return _public_s3_url_for(new_key)


def _resolve_external_thumb_to_s3_url(key: str) -> str | None:
    """Return a direct S3 URL for a ``/api/videos/external-thumb/<key>`` URL,
    uploading the cached composited JPEG on the fly if needed."""
    try:
        from integrations.video_thumb import (
            get_external_thumb_s3_key as _key_fn,
            get_cached_external_thumb_path as _path_fn,
            _s3_external_thumb_key as _build_key,
        )
    except Exception as e:
        logger.warning(f"[rewrite-s3] external-thumbs: helpers unavailable: {e}")
        return None
    try:
        s3_key = _key_fn(key)
    except Exception:
        s3_key = ""
    if s3_key:
        return _public_s3_url_for(s3_key)
    # Cache file may exist locally but not yet on S3 — re-upload from disk.
    try:
        path = _path_fn(key)
    except Exception:
        path = ""
    if not path:
        return None
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except Exception as e:
        logger.warning(f"[rewrite-s3] external-thumbs: read failed for {key}: {e}")
        return None
    deterministic_key = _build_key(key)
    new_key, _new_url = _try_upload_to_s3(
        kind="external-thumbs",
        key_hint=deterministic_key,
        raw=raw,
        content_type="image/jpeg",
    )
    if not new_key:
        return None
    return _public_s3_url_for(new_key)


# Match an ``/api/...`` path inside a URL (with or without scheme/host),
# stopping at the natural URL terminators. Anchored with a leading ``/``
# so it picks up both bare paths and absolute URLs.
import re as _re_rewrite

_RE_HOSTED_IMG = _re_rewrite.compile(r'/api/publish/image/hosted/([a-fA-F0-9]+)')
_RE_FEATURE_IMG = _re_rewrite.compile(r'/api/publish/image/serve/([A-Za-z0-9_\-]+)')
_RE_VIDEO_THUMB = _re_rewrite.compile(r'/api/videos/([A-Za-z0-9\-]{8,})/thumb')
_RE_EXT_THUMB = _re_rewrite.compile(r'/api/videos/external-thumb/([a-fA-F0-9]+)')
# Video-body matcher: ``/api/videos/<id>`` NOT followed by ``/thumb`` or
# ``/external-thumb`` (those have their own matchers). Ends at a quote,
# whitespace, ``<``, ``?``, ``#`` or end-of-string.
_RE_VIDEO_BODY = _re_rewrite.compile(
    r'/api/videos/([A-Za-z0-9\-]{8,})(?=$|[?#"\'\s<>])'
)

# Match URLs that appear inside ``<img src="..."``, ``<source src="..."``,
# or ``<video poster="...">`` attributes. We deliberately leave
# ``<a href="...">`` unchanged — the click-through link can keep
# pointing at the Replit app.
#
# The ``<video poster>`` arm is future-proofing: today no rendered email
# emits it (we use ``<img>`` for video posters), but if a template ever
# starts using HTML5 video posters directly, those URLs are already
# covered by the same rewrite.
_RE_IMG_SRC = _re_rewrite.compile(
    r'(<(?:img|source)\b[^>]*?\bsrc=)(["\'])([^"\']+)(\2)',
    flags=_re_rewrite.IGNORECASE,
)
_RE_VIDEO_POSTER = _re_rewrite.compile(
    r'(<video\b[^>]*?\bposter=)(["\'])([^"\']+)(\2)',
    flags=_re_rewrite.IGNORECASE,
)


def _rewrite_one_url(url: str) -> str:
    """Return a direct S3 URL for ``url`` if it matches one of the
    Replit-hosted patterns and we can resolve / upload it. Otherwise
    return ``url`` unchanged.
    """
    m = _RE_HOSTED_IMG.search(url)
    if m:
        new = _resolve_hosted_image_to_s3_url(m.group(1))
        return new or url
    m = _RE_FEATURE_IMG.search(url)
    if m:
        new = _resolve_feature_image_to_s3_url(m.group(1))
        return new or url
    m = _RE_VIDEO_THUMB.search(url)
    if m:
        new = _resolve_video_thumb_to_s3_url(m.group(1))
        return new or url
    m = _RE_EXT_THUMB.search(url)
    if m:
        new = _resolve_external_thumb_to_s3_url(m.group(1))
        return new or url
    m = _RE_VIDEO_BODY.search(url)
    if m:
        new = _resolve_video_body_to_s3_url(m.group(1))
        return new or url
    return url


def rewrite_email_html_to_direct_s3(html: str) -> str:
    """Walk every ``<img src>`` / ``<source src>`` URL in ``html`` and
    rewrite Replit-hosted asset URLs to direct, long-lived S3 public URLs.

    Returns the (possibly modified) HTML. Always returns a string of the
    same length-or-greater (we only swap URLs). When the S3 backend isn't
    enabled the rewrite is a no-op so local-mode previews keep working.
    Per-asset failures (missing rows, S3 outage, ...) leave that one URL
    unchanged so a single bad asset never breaks the whole email render.
    """
    if not html:
        return html
    try:
        from integrations import attachment_store as _astore
        if not _astore.s3_enabled():
            return html
    except Exception:
        return html

    rewritten_count = [0]
    skipped_count = [0]

    def _replace(match):
        prefix, quote, url, _close = match.group(1), match.group(2), match.group(3), match.group(4)
        new_url = _rewrite_one_url(url)
        if new_url != url:
            rewritten_count[0] += 1
        elif _RE_HOSTED_IMG.search(url) or _RE_FEATURE_IMG.search(url) \
                or _RE_VIDEO_THUMB.search(url) or _RE_VIDEO_BODY.search(url) \
                or _RE_EXT_THUMB.search(url):
            skipped_count[0] += 1
        return f"{prefix}{quote}{new_url}{quote}"

    out = _RE_IMG_SRC.sub(_replace, html)
    out = _RE_VIDEO_POSTER.sub(_replace, out)
    if rewritten_count[0] or skipped_count[0]:
        logger.info(
            f"[rewrite-s3] direct-S3 rewrite done: rewrote={rewritten_count[0]} "
            f"skipped={skipped_count[0]} (skipped left as-is so the in-app "
            f"serve route still handles them)"
        )
    return out


def _build_cid_attachments(images: dict) -> tuple:
    import re as _re, base64 as _b64
    cid_map = {}
    attachments = []
    if not images:
        return cid_map, attachments
    for idx, (img_name, data_url) in enumerate(images.items()):
        if not data_url or not data_url.startswith("data:"):
            continue
        m = _re.match(r"data:image/(\w+);base64,(.+)", data_url)
        if not m:
            continue
        ext = m.group(1)
        b64_data = m.group(2)
        try:
            decoded = list(_b64.b64decode(b64_data))
        except Exception:
            logger.warning(f"[email] Skipping image '{img_name}': invalid base64 data")
            continue
        cid = f"ampimg{idx}"
        cid_map[img_name] = cid
        attachments.append({
            "filename": img_name or f"image{idx}.{ext}",
            "content": decoded,
            "content_type": f"image/{ext}",
            "content_id": cid,
        })
    return cid_map, attachments


def _get_video_thumbnail(url: str) -> str:
    import re as _re
    yt = _re.search(r'(?:youtube\.com/(?:watch\?v=|embed/|shorts/)|youtu\.be/)([\w-]{11})', url)
    if yt:
        return f'https://img.youtube.com/vi/{yt.group(1)}/hqdefault.jpg'
    vimeo = _re.search(r'vimeo\.com/(\d+)', url)
    if vimeo:
        return f'https://vumbnail.com/{vimeo.group(1)}.jpg'
    # Loom: public sessions expose a poster image at this CDN path. The
    # email body still uses the share URL as the click-through link.
    loom = _re.search(r'loom\.com/(?:share|embed)/([A-Za-z0-9]+)', url)
    if loom:
        return f'https://cdn.loom.com/sessions/thumbnails/{loom.group(1)}-with-play.jpg'
    # Google Drive video file: Drive renders a poster via the thumbnail
    # endpoint regardless of file type, so it works for both images and
    # videos. Match the same three URL shapes the frontend's
    # _normalizeVideoUrl and getVideoThumbnail accept so the in-app
    # preview and the rendered email show the same picture.
    drive_id = None
    drv_a = _re.search(r'drive\.google\.com/file/d/([A-Za-z0-9_-]+)', url)
    if drv_a:
        drive_id = drv_a.group(1)
    if not drive_id:
        drv_b = _re.search(r'drive\.google\.com/open\?id=([A-Za-z0-9_-]+)', url)
        if drv_b:
            drive_id = drv_b.group(1)
    if not drive_id and 'drive.google.com' in url:
        drv_c = _re.search(r'[?&]id=([A-Za-z0-9_-]+)', url)
        if drv_c:
            drive_id = drv_c.group(1)
    if drive_id:
        return f'https://drive.google.com/thumbnail?id={drive_id}&sz=w1600'
    # Local fallback served by /api/placeholder/video-thumb. We embed an
    # absolute URL since this string ends up in delivered email HTML.
    try:
        return f"{_get_base_url().rstrip('/')}/api/placeholder/video-thumb"
    except Exception:
        return '/api/placeholder/video-thumb'


_VIDEO_ATTACH_MAX_TOTAL = 22 * 1024 * 1024  # keep under Gmail's 25MB inbound cap (with HTML headroom)


def _build_video_attachments(video_map: dict) -> tuple:
    """Build Resend attachment dicts for locally-uploaded videos.

    Skips external URLs (YouTube/Vimeo) and caps total payload size.

    Returns ``(attachments, skipped)`` where ``skipped`` is a list of dicts
    describing locally-stored videos that could not be attached because they
    would exceed the per-email size cap. Each skipped entry has::

        {"filename": str, "size_bytes": int, "cap_bytes": int}
    """
    if not video_map:
        return [], []
    try:
        from ai.publish_store import get_video_path
    except Exception:
        return [], []
    import os as _os
    import re as _re
    attachments = []
    skipped = []
    total = 0
    seen_ids = set()
    seen_names = set()
    ctype_by_ext = {
        "mp4": "video/mp4",
        "m4v": "video/mp4",
        "mov": "video/quicktime",
        "webm": "video/webm",
        "avi": "video/x-msvideo",
    }
    for fname, info in (video_map or {}).items():
        vurl = ((info or {}).get("video_url") or "").strip()
        m = _re.search(r'/api/videos/([A-Za-z0-9\-]{8,})(?:$|[/?#])', vurl)
        if not m:
            continue
        vid_id = m.group(1)
        if vid_id in seen_ids:
            continue
        seen_ids.add(vid_id)
        try:
            result = get_video_path(vid_id)
        except Exception:
            continue
        if isinstance(result, tuple):
            path = result[0]
            vmeta = result[1] if len(result) > 1 else None
        else:
            path = result
            vmeta = None
        if not path or not _os.path.exists(path):
            continue
        stored_ext = (vmeta or {}).get("ext", "") if isinstance(vmeta, dict) else ""
        is_normalized_mp4 = (stored_ext == ".mp4") or path.lower().endswith(".mp4")
        try:
            size = _os.path.getsize(path)
        except Exception:
            continue
        if total + size > _VIDEO_ATTACH_MAX_TOTAL:
            logger.warning(
                f"[email] Skipping video attachment '{fname}' ({size} bytes) — "
                f"would exceed {_VIDEO_ATTACH_MAX_TOTAL} byte cap"
            )
            skipped.append({
                "filename": fname or f"{vid_id}.mp4",
                "size_bytes": int(size),
                "cap_bytes": int(_VIDEO_ATTACH_MAX_TOTAL),
            })
            continue
        try:
            with open(path, "rb") as f:
                data = f.read()
        except Exception as e:
            logger.warning(f"[email] Could not read video '{fname}': {e}")
            continue
        safe_name = fname or f"{vid_id}.mp4"
        if is_normalized_mp4:
            stem = _os.path.splitext(safe_name)[0] or vid_id
            safe_name = f"{stem}.mp4"
        if safe_name in seen_names:
            stem, dot, ext = safe_name.rpartition(".")
            safe_name = f"{stem or safe_name}-{vid_id[:6]}{dot}{ext}" if dot else f"{safe_name}-{vid_id[:6]}"
        seen_names.add(safe_name)
        ext = _os.path.splitext(safe_name)[1].lower().lstrip(".")
        content_type = "video/mp4" if is_normalized_mp4 else ctype_by_ext.get(ext, "video/mp4")
        attachments.append({
            "filename": safe_name,
            "content": list(data),
            "content_type": content_type,
        })
        total += size
        logger.info(f"[email] Attached video '{safe_name}' ({size} bytes) to email")
    return attachments, skipped


def _composited_external_thumb_url(remote_thumb_url: str) -> str:
    """Fetch + composite external thumbnail, serve from our own URL.

    Falls back to the raw remote URL if compositing fails.
    """
    try:
        from integrations.video_thumb import get_cached_external_thumb
        key = get_cached_external_thumb(remote_thumb_url)
        if not key:
            return remote_thumb_url
        return f"{_get_base_url()}/api/videos/external-thumb/{key}"
    except Exception:
        return remote_thumb_url


def _normalize_video_key(name: str) -> str:
    """Normalize a video filename for fuzzy marker lookup.

    The same video can show up under slightly different names because:
      * Browsers append ``(1)``, ``(2)``... when downloading a duplicate.
      * Recipients re-cap or re-space the filename when pasting it back
        into the editor.
      * Names round-trip through systems that collapse Unicode whitespace
        (e.g. ``\u202f`` in macOS screen-recording filenames).

    Two names are considered equivalent if they share the same lower-cased,
    whitespace-collapsed stem after stripping any trailing ``(N)`` browser
    dedup suffix and the file extension. Returns ``""`` for empty input so
    callers can short-circuit cheaply.
    """
    if not name:
        return ""
    import os as _os
    import re as _re
    stem, ext = _os.path.splitext(str(name))
    stem = _re.sub(r'\s*\(\d+\)\s*$', '', stem)
    stem = _re.sub(r'\s+', ' ', stem).strip().lower()
    ext = (ext or "").strip().lower()
    return stem + ext


def _resolve_video_marker(vid_ref: str, video_map: dict) -> tuple:
    """Look up a ``[video: name]`` marker against ``video_map``.

    Tries exact match first (the common case). Falls back to a normalized
    match (see ``_normalize_video_key``) so a body marker like
    ``Shortlist.mp4`` still resolves a video uploaded as
    ``Shortlist (1).mp4``. As a last resort, when the body's marker maps
    to nothing but only a single video has been attached, returns that
    single video — the user's intent is unambiguous in that case and a
    silent "not attached" stub is far worse than rendering the one
    obvious match.

    Returns ``(matched_key, vid_info)`` on success or ``(None, None)``.
    """
    if not vid_ref or not video_map:
        return (None, None)
    if vid_ref in video_map:
        return (vid_ref, video_map[vid_ref])
    norm_ref = _normalize_video_key(vid_ref)
    if norm_ref:
        for k, v in video_map.items():
            if _normalize_video_key(k) == norm_ref:
                return (k, v)
    if len(video_map) == 1:
        only_key = next(iter(video_map))
        return (only_key, video_map[only_key])
    return (None, None)


def render_email_html(subject: str, body: str, images: dict = None, cid_map: dict = None, from_name: str = None, videos: dict = None, strict: bool = False, view_url: str = None, unsubscribe_placeholder: str = None) -> str:
    """Render an email body to HTML.

    When ``strict`` is True, raise ``MediaResolutionError`` if any
    ``[image: ...]`` or ``[video: ...]`` marker in the body cannot be
    resolved to a hosted asset. The send path uses strict=True so a
    broken email never goes out; the preview path uses strict=False so
    the editor can still see what's missing.
    """
    _ = from_name
    import re
    safe_subject = _esc(subject)
    image_map = images or {}
    video_map = videos or {}
    missing_images: list = []
    missing_videos: list = []

    banner_month = _current_month_year()
    banner_title = "Product Updates"
    body_text = body or ""
    banner_match = re.search(r'^\[banner:\s*(?:title=([^|;\]]+))?(?:[|;]\s*month=([^\]]+))?\]\s*$', body_text, flags=re.MULTILINE | re.IGNORECASE)
    if banner_match:
        if banner_match.group(1):
            banner_title = banner_match.group(1).strip()
        if banner_match.group(2):
            banner_month = banner_match.group(2).strip()
        body_text = body_text.replace(banner_match.group(0), "", 1)

    in_list = False
    pending_chip_html = ""
    lines = body_text.strip().split("\n")
    body_html = ""
    has_banner = bool(banner_match)
    first_text_done = has_banner

    def close_list():
        nonlocal in_list, body_html
        if in_list:
            body_html += '</ul>'
            in_list = False

    for line in lines:
        stripped = line.strip()

        chip_match = re.match(r'^\[badge:\s*(.+?)\]$', stripped, re.IGNORECASE)
        if chip_match:
            close_list()
            pending_chip_html = _render_chip_html(chip_match.group(1))
            continue

        callout_match = re.match(r'^>\s*(.+)$', stripped)
        if callout_match:
            close_list()
            if pending_chip_html:
                body_html += pending_chip_html
                pending_chip_html = ""
            body_html += _render_callout_html(callout_match.group(1))
            first_text_done = True
            continue

        cta_match = re.match(r'^\[cta:\s*(?:text=([^|;\]]+))?(?:[|;]\s*url=([^\]]+))?\]$', stripped, re.IGNORECASE)
        if cta_match:
            close_list()
            if pending_chip_html:
                body_html += pending_chip_html
                pending_chip_html = ""
            cta_text = (cta_match.group(1) or "Learn more").strip()
            cta_url = (cta_match.group(2) or "").strip()
            if cta_url:
                if not re.match(r'^(https?:|mailto:)', cta_url, re.IGNORECASE):
                    cta_url = 'https://' + cta_url
                body_html += (
                    '<div style="text-align:center;margin:20px 0 24px 0;">'
                    f'<a href="{_esc(cta_url)}" target="_blank" rel="noopener noreferrer" '
                    'style="display:inline-block;background:#1a1d23;color:#ffffff;'
                    'text-decoration:none;font-weight:600;font-size:14px;line-height:1;'
                    'padding:12px 24px;border-radius:6px;mso-padding-alt:0;">'
                    f'{_esc(cta_text)}</a></div>'
                )
                first_text_done = True
            continue

        if re.match(r'^(---+|___+|\*\*\*+)$', stripped):
            close_list()
            if pending_chip_html:
                body_html += pending_chip_html
                pending_chip_html = ""
            body_html += '<hr style="border:none;border-top:1px solid #e8e8eb;margin:24px 0;">'
            continue

        if not stripped:
            close_list()
            body_html += "<br>"
        elif not first_text_done and not re.match(r'^\[image:\s*(.+)\]$', stripped) and not re.match(r'^\[video:\s*(.+)\]$', stripped) and not stripped.startswith('#'):
            close_list()
            first_text_done = True
            if pending_chip_html:
                body_html += pending_chip_html
                pending_chip_html = ""
            body_html += f'<h2 style="margin:0 0 16px 0;color:#1a1d23;font-size:22px;font-weight:700;line-height:1.3;">{_inline_markdown(stripped)}</h2>'
        elif re.match(r'^\[video:\s*(.+)\]$', stripped, re.IGNORECASE):
            vid_ref = re.match(r'^\[video:\s*(.+)\]$', stripped, re.IGNORECASE).group(1).strip()
            if re.match(r'^https?://', vid_ref, re.IGNORECASE):
                remote_thumb = _get_video_thumbnail(vid_ref)
                thumb_url = _composited_external_thumb_url(remote_thumb)
                vid_link = vid_ref
            else:
                _matched_key, vid_info = _resolve_video_marker(vid_ref, video_map)
                if vid_info is not None:
                    thumb_url = vid_info.get("thumb_url", "")
                    vid_link = vid_info.get("video_url", "")
                else:
                    missing_videos.append(vid_ref)
                    body_html += f'<p style="margin:0 0 12px 0;color:#b91c1c;font-size:13px;font-weight:600;">[Video: {_esc(vid_ref)}] &mdash; not attached</p>'
                    continue
            if not thumb_url or not vid_link:
                # Entry exists but is incomplete (missing thumb_url or
                # video_url). Treat the same as a missing marker so strict
                # mode blocks send instead of shipping a placeholder line.
                missing_videos.append(vid_ref)
                body_html += f'<p style="margin:0 0 12px 0;color:#b91c1c;font-size:13px;font-weight:600;">[Video: {_esc(vid_ref)}] &mdash; thumbnail or link unavailable</p>'
                continue
            esc_link = _esc(vid_link)
            esc_thumb = _esc(thumb_url)
            # Single-layer poster + click-through link with a play-button
            # overlay. We previously stacked a `<video controls>` element
            # on top so Apple Mail could play inline, but that overlay
            # rendered as a black box covering the poster in the in-app
            # preview iframe (and in any client where `<video>` shows but
            # the source can't be loaded inline). The click-through pattern
            # used here matches the in-app channel preview, works in every
            # email client, and never hides the poster.
            body_html += (
                f'<div style="text-align:center;margin:16px 0 20px 0;">'
                f'<a href="{esc_link}" target="_blank" rel="noopener noreferrer" '
                f'style="display:inline-block;position:relative;text-decoration:none;max-width:600px;width:100%;line-height:0;">'
                f'<img src="{esc_thumb}" alt="Play video" width="600" '
                f'style="display:block;width:100%;max-width:600px;height:auto;border-radius:6px;border:0;outline:none;">'
                f'<span style="position:absolute;top:50%;left:50%;'
                f'transform:translate(-50%,-50%);width:64px;height:64px;'
                f'background:rgba(0,0,0,0.65);border-radius:50%;'
                f'display:inline-block;line-height:64px;text-align:center;'
                f'font-size:0;">'
                f'<span style="display:inline-block;vertical-align:middle;'
                f'width:0;height:0;border-style:solid;'
                f'border-width:14px 0 14px 22px;'
                f'border-color:transparent transparent transparent #ffffff;'
                f'margin-left:6px;"></span>'
                f'</span>'
                f'</a>'
                f'</div>'
            )
        elif re.match(r'^\[image:\s*(.+)\]$', stripped):
            img_name = re.match(r'^\[image:\s*(.+)\]$', stripped).group(1).strip()
            if re.match(r'^https?://', img_name, re.IGNORECASE):
                body_html += f'<div style="margin:16px 0;"><img src="{_esc(img_name)}" alt="Image" style="max-width:100%;height:auto;border-radius:6px;display:block;"></div>'
            elif cid_map and img_name in cid_map:
                cid = cid_map[img_name]
                body_html += f'<div style="margin:16px 0;"><img src="cid:{cid}" alt="{_esc(img_name)}" style="max-width:100%;height:auto;border-radius:6px;display:block;"></div>'
            elif image_map.get(img_name):
                img_src = image_map[img_name]
                body_html += f'<div style="margin:16px 0;"><img src="{_esc(img_src)}" alt="{_esc(img_name)}" style="max-width:100%;height:auto;border-radius:6px;display:block;"></div>'
            else:
                missing_images.append(img_name)
                body_html += f'<p style="margin:0 0 12px 0;color:#b91c1c;font-size:13px;font-weight:600;">[Image: {_esc(img_name)}] &mdash; not attached</p>'
        elif re.match(r'^#{1,3}\s+', stripped):
            close_list()
            first_text_done = True
            hm = re.match(r'^(#{1,3})\s+(.+)$', stripped)
            if hm:
                level = len(hm.group(1))
                sizes = {1: '24px', 2: '20px', 3: '17px'}
                top_margin = '24px' if body_html else '0'
                if pending_chip_html:
                    body_html += pending_chip_html
                    pending_chip_html = ""
                    top_margin = '0'
                body_html += f'<h{level} style="margin:{top_margin} 0 12px 0;color:#1a1d23;font-size:{sizes[level]};font-weight:700;line-height:1.3;">{_inline_markdown(hm.group(2))}</h{level}>'
            else:
                body_html += f'<p style="margin:0 0 12px 0;color:#333333;font-size:15px;line-height:1.6;">{_inline_markdown(stripped)}</p>'
        elif stripped.startswith("- "):
            if not in_list:
                body_html += '<ul style="margin:8px 0 14px 0;padding-left:22px;">'
                in_list = True
            body_html += f'<li style="margin-bottom:6px;color:#333333;font-size:15px;line-height:1.6;">{_inline_markdown(stripped[2:])}</li>'
        else:
            close_list()
            if pending_chip_html:
                body_html += pending_chip_html
                pending_chip_html = ""
            # Auto-promotion to a green CTA button is intentionally narrow.
            # Previously any paragraph that contained a verb phrase ("explore",
            # "check it out", "learn more", ...) AND a URL was rewritten into
            # a full-width button. That collided with the AI prompt, which
            # tells the model to end the body with an INLINE hyperlink
            # sentence ("Explore it on [the Spotify Playlists page](URL).")
            # and produced a duplicate button.
            #
            # Only treat the line as a button when the user/AI gave an
            # unambiguous button signal: a line that is JUST `[Label](url)`
            # (optionally with a trailing period). Any prose-with-link stays
            # as a normal paragraph, and explicit `[cta: ...]` blocks still
            # render as buttons via the earlier branch.
            standalone_link = re.match(r'^\[([^\]]+)\]\((https?://[^)]+)\)\s*\.?$', stripped)
            if standalone_link is not None:
                url = _esc(standalone_link.group(2))
                label = _esc(standalone_link.group(1).strip() or "Try it now")
                body_html += f'<div style="text-align:center;margin:24px 0;"><a href="{url}" style="display:inline-block;background:#00C9A7;color:#ffffff;text-decoration:none;padding:12px 32px;border-radius:6px;font-weight:700;font-size:15px;">{label}</a></div>'
            else:
                body_html += f'<p style="margin:0 0 12px 0;color:#333333;font-size:15px;line-height:1.6;">{_inline_markdown(stripped)}</p>'

    close_list()
    if pending_chip_html:
        body_html += pending_chip_html

    if strict and (missing_images or missing_videos):
        logger.warning(
            f"[email] strict render rejected send: missing_images={missing_images!r} "
            f"missing_videos={missing_videos!r}"
        )
        raise MediaResolutionError(missing_images, missing_videos)

    safe_banner_title = _esc(banner_title)
    safe_banner_month = _esc(banner_month)

    # Dark-mode support: we declare `color-scheme` so iOS Mail / Apple Mail
    # don't apply their aggressive auto-invert (which was turning the white
    # banner text into illegible mud against the dark gradient). We then
    # provide explicit overrides under both `prefers-color-scheme: dark`
    # (for real recipients on dark-mode clients) and a `.amplify-force-dark`
    # wrapper class (so the in-app Live Preview's Dark toggle can simulate
    # the same rendering even when the user's OS is in light mode).
    dark_mode_style_block = """<style>
@media (prefers-color-scheme: dark) {
  .amplify-outer-bg { background:#0f1115 !important; }
  .amplify-body-card { background:#1a1d23 !important; }
  .amplify-body-card p, .amplify-body-card li { color:#e5e7eb !important; }
  .amplify-body-card h1, .amplify-body-card h2, .amplify-body-card h3 { color:#ffffff !important; }
  .amplify-body-card hr { border-top-color:#2d3138 !important; }
  .amplify-body-card a { color:#5eead4 !important; }
  .amplify-footer-meta { color:#a0a4ad !important; }
  .amplify-footer-link { color:#a0a4ad !important; }
  .amplify-banner-title { color:#ffffff !important; }
  .amplify-banner-month { color:#9ae6d4 !important; }
}
.amplify-force-dark .amplify-outer-bg { background:#0f1115 !important; }
.amplify-force-dark .amplify-body-card { background:#1a1d23 !important; }
.amplify-force-dark .amplify-body-card p, .amplify-force-dark .amplify-body-card li { color:#e5e7eb !important; }
.amplify-force-dark .amplify-body-card h1, .amplify-force-dark .amplify-body-card h2, .amplify-force-dark .amplify-body-card h3 { color:#ffffff !important; }
.amplify-force-dark .amplify-body-card hr { border-top-color:#2d3138 !important; }
.amplify-force-dark .amplify-body-card a { color:#5eead4 !important; }
.amplify-force-dark .amplify-footer-meta { color:#a0a4ad !important; }
.amplify-force-dark .amplify-footer-link { color:#a0a4ad !important; }
.amplify-force-dark .amplify-banner-title { color:#ffffff !important; }
.amplify-force-dark .amplify-banner-month { color:#9ae6d4 !important; }
</style>"""

    banner_row = f"""<tr><td style="background:linear-gradient(135deg,#0f172a 0%,#1a1d23 60%,#0b3b33 100%);padding:32px;border-bottom:3px solid #00C9A7;">
<div class="amplify-banner-month" style="color:#9ae6d4;font-size:11px;font-weight:700;letter-spacing:1.2px;text-transform:uppercase;margin-bottom:6px;">{safe_banner_month}</div>
<div class="amplify-banner-title" style="color:#ffffff;font-size:26px;font-weight:800;letter-spacing:-0.5px;line-height:1.2;">{safe_banner_title}</div>
</td></tr>""" if has_banner else ""
    header_radius = "8px 8px 0 0" if has_banner else "8px 8px 0 0"
    body_radius = "0 0 8px 8px"

    final_html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="color-scheme" content="light dark">
<meta name="supported-color-schemes" content="light dark">
{dark_mode_style_block}
</head>
<body class="amplify-outer-bg" style="margin:0;padding:0;background:#f4f4f7;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" class="amplify-outer-bg" style="background:#f4f4f7;padding:24px 0;">
<tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;">
<tr><td style="background:#1a1d23;padding:18px 32px;border-radius:{header_radius};">
<span style="color:#ffffff;font-size:20px;font-weight:700;letter-spacing:-0.3px;">Chartmetric</span>
</td></tr>
{banner_row}
<tr><td class="amplify-body-card" style="background:#ffffff;padding:32px;border-radius:{body_radius};">
{body_html}
<hr style="border:none;border-top:1px solid #e8e8eb;margin:28px 0 16px 0;">
{_render_footer_links_html(view_url, unsubscribe_placeholder)}
<p class="amplify-footer-meta" style="margin:0;color:#999999;font-size:12px;line-height:1.6;">&copy; {_current_year()} Chartmetric, Inc.</p>
</td></tr>
</table>
</td></tr>
</table>
</body>
</html>"""

    # Task #115: rewrite every ``<img src>`` / ``<source src>`` URL pointing
    # at the Replit app (``/api/publish/image/...``, ``/api/videos/...``)
    # to the corresponding direct, long-lived public S3 URL. Makes the
    # downloaded HTML self-contained so it keeps rendering even if the app
    # is offline. No-op when S3 backend isn't enabled.
    try:
        final_html = rewrite_email_html_to_direct_s3(final_html)
    except Exception as e:
        logger.warning(
            f"[email] direct-S3 rewrite raised unexpectedly; "
            f"returning unrewritten HTML: {e}"
        )
    return final_html


def _send_via_resend(subject: str, html_content: str, to_emails: list, is_test: bool, attachments: list = None, from_name: str = None, template_id: str = None, bcc_emails: list = None, audience_id: str = None, topic_id: str = None) -> dict | None:
    import time as _time
    resend_api_key = os.environ.get("RESEND_API_KEY", "")
    from_email = os.environ.get("RESEND_FROM_EMAIL", "") or os.environ.get("SENDGRID_FROM_EMAIL", "")
    if not resend_api_key or not from_email:
        return None

    import resend
    resend.api_key = resend_api_key

    display_name = from_name or "Chartmetric"
    sender = f"{display_name} <{from_email}>"
    aud_id_clean = (audience_id or "").strip()
    topic_id_clean = (topic_id or "").strip()
    has_placeholder = UNSUBSCRIBE_PLACEHOLDER in (html_content or "")
    email_list = []
    for addr in to_emails:
        params = {
            "from": sender,
            "to": [addr],
            "subject": subject,
        }
        per_recipient_html = html_content
        # Unsubscribe handling has two modes:
        #   * Audience send: per-recipient signed URL that flips the
        #     contact's topic subscription (or the audience-wide flag
        #     when there's no topic_id). Personal token => one TO per
        #     params object, never co-mingled with BCC.
        #   * Custom typed-in recipients / test sends: no audience to
        #     flip, so we substitute the generic
        #     :data:`GENERIC_UNSUBSCRIBE_MAILTO` and surface it via the
        #     List-Unsubscribe header. This keeps the email CAN-SPAM
        #     compliant even when nobody picked an audience in the UI.
        if aud_id_clean:
            unsub_url = build_unsubscribe_url(aud_id_clean, addr, topic_id=topic_id_clean)
            if unsub_url:
                if has_placeholder and per_recipient_html:
                    per_recipient_html = per_recipient_html.replace(UNSUBSCRIBE_PLACEHOLDER, unsub_url)
                # RFC 2369 + RFC 8058: List-Unsubscribe-Post enables Gmail's
                # one-click unsubscribe button at the top of the message.
                params["headers"] = {
                    "List-Unsubscribe": f"<{unsub_url}>",
                    "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
                }
            else:
                # SESSION_SECRET missing/insecure: we can't mint a signed
                # audience link, but the recipient still deserves a way
                # to opt out, so fall back to the generic mailto.
                if has_placeholder and per_recipient_html:
                    per_recipient_html = per_recipient_html.replace(UNSUBSCRIBE_PLACEHOLDER, GENERIC_UNSUBSCRIBE_MAILTO)
                params["headers"] = {
                    "List-Unsubscribe": f"<{GENERIC_UNSUBSCRIBE_MAILTO}>",
                }
            # NOTE: do NOT attach BCC to per-recipient audience sends.
            # If we did, every BCC inbox would receive N copies (one per
            # TO recipient), each carrying a different recipient's signed
            # unsubscribe token in the footer and List-Unsubscribe header.
            # A BCC viewer clicking that link would unsubscribe the wrong
            # contact (token confusion). BCC for audience sends is handled
            # below as a single archival copy.
        else:
            # Custom typed-in recipients or test sends: ship the generic
            # mailto: opt-out so the email isn't dead-ended. RFC 2369
            # allows a mailto: scheme in List-Unsubscribe; we omit
            # List-Unsubscribe-Post because that header is HTTPS-only
            # per RFC 8058.
            if has_placeholder and per_recipient_html:
                per_recipient_html = per_recipient_html.replace(UNSUBSCRIBE_PLACEHOLDER, GENERIC_UNSUBSCRIBE_MAILTO)
            params["headers"] = {
                "List-Unsubscribe": f"<{GENERIC_UNSUBSCRIBE_MAILTO}>",
            }
            if bcc_emails:
                params["bcc"] = bcc_emails
        if template_id:
            params["template"] = {"id": template_id}
        else:
            params["html"] = per_recipient_html
        if attachments:
            params["attachments"] = attachments
        email_list.append(params)

    # Audience-mode BCC archival copy: one message with the BCC list in BCC
    # (preserving BCC privacy) and the from address as the TO. The body is
    # the original HTML with the unsubscribe placeholder swapped to the
    # static fallback URL so we never ship a literal placeholder.
    if aud_id_clean and bcc_emails:
        # Archival copy is a single shared message — we can't embed any one
        # recipient's signed unsubscribe URL, so fall back to the generic
        # mailto so the placeholder never ships as literal text.
        archival_html = (html_content or "").replace(
            UNSUBSCRIBE_PLACEHOLDER, GENERIC_UNSUBSCRIBE_MAILTO
        )
        archival_params = {
            "from": sender,
            "to": [from_email],
            "bcc": list(bcc_emails),
            "subject": subject,
        }
        if template_id:
            archival_params["template"] = {"id": template_id}
        else:
            archival_params["html"] = archival_html
        if attachments:
            archival_params["attachments"] = attachments
        email_list.append(archival_params)

    max_retries = 3
    for attempt in range(max_retries):
        try:
            if len(email_list) == 1:
                email_resp = resend.Emails.send(email_list[0])
                ids = [email_resp.get("id", "") if isinstance(email_resp, dict) else getattr(email_resp, "id", "")]
            else:
                batch_resp = resend.Batch.send(email_list)
                if isinstance(batch_resp, dict):
                    ids = [item.get("id", "") for item in batch_resp.get("data", [])]
                elif isinstance(batch_resp, list):
                    ids = [getattr(item, "id", "") if not isinstance(item, dict) else item.get("id", "") for item in batch_resp]
                else:
                    ids = [str(batch_resp)]
            to_str = ", ".join(to_emails)
            bcc_str = ", ".join(bcc_emails) if bcc_emails else ""
            logger.info(f"[resend] Sent {len(to_emails)} individual email(s) to {to_str}, ids={ids}")
            return {
                "success": True,
                "method": "resend",
                "message_id": ids[0] if ids else "",
                "to": to_str,
                "bcc": bcc_str,
                "bcc_count": len(bcc_emails) if bcc_emails else 0,
                "count": len(to_emails),
                "is_test": is_test,
            }
        except Exception as e:
            err_str = str(e).lower()
            if "rate" in err_str and "limit" in err_str and attempt < max_retries - 1:
                wait = 2 ** attempt
                logger.warning(f"[resend] Rate limited, retrying in {wait}s (attempt {attempt + 1}/{max_retries})")
                _time.sleep(wait)
                continue
            _resp_body = ""
            for _attr in ("body", "response", "message", "args"):
                _v = getattr(e, _attr, None)
                if _v:
                    _resp_body = repr(_v)[:600]
                    break
            logger.exception(
                f"[resend] Send failed: type={type(e).__name__} str={e!s} repr={e!r} "
                f"attr_body={_resp_body} batch_size={len(email_list)} attempt={attempt + 1}/{max_retries} "
                f"to={to_emails} from={sender!r} template_id={email_list[0].get('template') if email_list else None} "
                f"has_attachments={bool(attachments)} attachment_count={len(attachments) if attachments else 0}"
            )
            return None
    return None


def send_email(subject: str, body: str, to_email: str = None, is_test: bool = True, images: dict = None, from_name: str = None, template_id: str = None, videos: dict = None, bcc_email: str = None, audience_id: str = None, topic_id: str = None) -> dict:
    resend_api_key = os.environ.get("RESEND_API_KEY", "")
    from_email = os.environ.get("RESEND_FROM_EMAIL", "") or os.environ.get("SENDGRID_FROM_EMAIL", "")
    test_email = os.environ.get("SENDGRID_TEST_EMAIL", "") or os.environ.get("RESEND_FROM_EMAIL", "")

    has_resend = bool(resend_api_key and from_email)

    if not has_resend:
        logger.warning("[email] No email provider configured (need RESEND_API_KEY+RESEND_FROM_EMAIL)")
        preview_html = render_email_html(subject, body, images=images, from_name=from_name, videos=videos)
        return {
            "success": True,
            "method": "fallback",
            "message": "Email draft ready. No email provider configured.",
            "subject": subject,
            "body": body,
            "preview_html": preview_html,
        }

    recipients_raw = to_email or (test_email if is_test else "")
    recipients = [e.strip() for e in recipients_raw.split(",") if e.strip()] if recipients_raw else []
    if not recipients:
        return {
            "success": False,
            "error": "No recipient email provided. Set SENDGRID_TEST_EMAIL or provide to_email.",
        }

    final_subject = f"[TEST] {subject}" if is_test else subject
    recipients_str = ", ".join(recipients)

    # Log the body markers we're about to render so we can compare them to the
    # image_map keys (Unicode mismatches between the marker text and the key
    # name silently turn an image into a "[Image: ...]" text placeholder).
    import re as _re_dbg
    _body_img_markers = [m.group(1).strip() for m in _re_dbg.finditer(r'\[image:\s*([^\]]+)\]', body or "")]
    _body_vid_markers = [m.group(1).strip() for m in _re_dbg.finditer(r'\[video:\s*([^\]]+)\]', body or "")]
    _img_keys = list((images or {}).keys())
    _vid_keys = list((videos or {}).keys())
    _LOG_CAP = 8
    logger.info(
        f"[email] send_email markers: "
        f"image_markers(n={len(_body_img_markers)})={[repr(x) for x in _body_img_markers[:_LOG_CAP]]} "
        f"video_markers(n={len(_body_vid_markers)})={[repr(x) for x in _body_vid_markers[:_LOG_CAP]]} "
        f"image_map_keys(n={len(_img_keys)})={[repr(k) for k in _img_keys[:_LOG_CAP]]} "
        f"video_map_keys(n={len(_vid_keys)})={[repr(k) for k in _vid_keys[:_LOG_CAP]]}"
    )
    _missing = [m for m in _body_img_markers if m not in (images or {})]
    if _missing:
        logger.warning(
            f"[email] {len(_missing)} body image marker(s) NOT in image_map "
            f"(will render as text placeholders): {[repr(x) for x in _missing[:_LOG_CAP]]}"
        )

    hosted_images = _build_hosted_image_map(images)
    bcc_list = [e.strip() for e in (bcc_email or "").split(",") if e.strip()] if bcc_email else None

    # Generate the "View in browser" token BEFORE rendering so the
    # rendered HTML's footer link can point at the same hosted page that
    # we're about to persist. The token is unguessable
    # (secrets.token_urlsafe(16)) so /email/view/<token> doesn't need
    # auth — only people who received the email or have the URL can read
    # it.
    import secrets as _secrets
    view_token = _secrets.token_urlsafe(16)
    view_url = _build_view_in_browser_url(view_token)

    aud_id_clean = (audience_id or "").strip()
    topic_id_clean = (topic_id or "").strip()
    # Always render an unsubscribe link in the footer (CAN-SPAM friendly).
    # _send_via_resend substitutes a personal signed URL when the send is
    # tied to an audience, or a generic mailto: opt-out address otherwise.
    unsub_placeholder = UNSUBSCRIBE_PLACEHOLDER

    # Per-topic opt-out filter (Task #77): drop recipients who have
    # opted out of this topic before we render or send. Topic state is
    # workspace-level in Resend, so a recipient who opted out of
    # "Product Update" in Audience A is also dropped from Audience B's
    # next "Product Update" send. The audience-wide ``unsubscribed``
    # flag is already filtered upstream in
    # ``/api/resend/audiences/<id>/contacts``; this is the second layer
    # specific to the topic.
    if aud_id_clean and topic_id_clean and recipients:
        before_n = len(recipients)
        topic_filter = filter_emails_by_topic_subscription(recipients, topic_id_clean)
        # Fail-safe: if any recipient could not be resolved against
        # Resend topics, abort the whole send. Sending to a partial
        # subset would risk emailing an opted-out contact.
        if not topic_filter.get("ok"):
            logger.error(
                f"[email] Topic filter could not resolve all recipients for topic "
                f"<{topic_id_clean[:8]}...>; aborting send. errors={topic_filter.get('errors')}"
            )
            return {
                "success": False,
                "error": topic_filter.get("error") or (
                    "Could not verify topic subscriptions. The send was blocked."
                ),
                "topic_filter_error": True,
            }
        recipients = topic_filter.get("kept") or []
        if len(recipients) < before_n:
            logger.info(
                f"[email] Topic filter dropped {before_n - len(recipients)} recipient(s) "
                f"opted out of topic <{topic_id_clean[:8]}...> "
                f"(kept {len(recipients)}/{before_n}, default={topic_filter.get('default_subscription')!r})"
            )
        if not recipients:
            logger.warning(
                f"[email] All recipients opted out of topic <{topic_id_clean[:8]}...>; nothing to send"
            )
            return {
                "success": False,
                "error": "Every recipient is opted out of this topic, so no email was sent.",
                "topic_filtered": True,
            }
        recipients_str = ", ".join(recipients)
    try:
        html_content = render_email_html(
            final_subject, body, images=hosted_images, from_name=from_name,
            videos=videos, strict=True, view_url=view_url,
            unsubscribe_placeholder=unsub_placeholder,
        )
    except MediaResolutionError as _mre:
        logger.warning(
            f"[email] BLOCKED send: {_mre} (recipients={recipients_str!r})"
        )
        return {
            "success": False,
            "error": str(_mre),
            "missing_images": _mre.missing_images,
            "missing_videos": _mre.missing_videos,
            "hint": (
                "Re-attach the missing media in the editor before sending. "
                "Each [image: name] / [video: name] marker must point at an "
                "attachment with the exact same name."
            ),
        }

    # Log every <img> tag in the rendered HTML so we can verify exactly what
    # URLs the email recipient (and Gmail's image proxy) will fetch.
    _img_tags = _re_dbg.findall(r'<img[^>]+src="([^"]+)"[^>]*>', html_content or "")
    logger.info(
        f"[email] outgoing HTML img tags: count={len(_img_tags)} "
        f"srcs={[s[:160] for s in _img_tags[:8]]}"
    )
    if videos:
        video_attachments, skipped_videos = _build_video_attachments(videos)
    else:
        video_attachments, skipped_videos = [], []
    result = _send_via_resend(final_subject, html_content, recipients, is_test, attachments=video_attachments or None, from_name=from_name, template_id=template_id, bcc_emails=bcc_list, audience_id=aud_id_clean or None, topic_id=topic_id_clean or None)
    if result:
        if skipped_videos:
            result["skipped_videos"] = skipped_videos
        # Persist the rendered HTML so the "View in browser" link in the
        # footer resolves to the same content the recipient just opened.
        # Save AFTER send so we don't host pages for emails that failed
        # to dispatch (the token in the HTML is now effectively dead in
        # that case, which is the desired behavior).
        if result.get("success"):
            # The hosted "View in browser" page is shared (one URL per send,
            # not per recipient), so we cannot embed a personal unsubscribe
            # token here. Substitute the generic mailto so the saved snapshot
            # never ships a literal "{{AMPLIFY_UNSUBSCRIBE_URL}}" and the
            # link still works for someone reading the page on the web.
            hosted_html = (html_content or "").replace(
                UNSUBSCRIBE_PLACEHOLDER, GENERIC_UNSUBSCRIBE_MAILTO
            )
            _save_hosted_email(view_token, hosted_html)
            result["view_url"] = view_url
            result["view_token"] = view_token
        return result

    return {
        "success": False,
        "error": "Resend send failed. Check API key and configuration.",
    }

    # --- SendGrid path (disabled — using Resend only) ---
    # html_content = render_email_html(final_subject, body, images=images)
    # if has_sendgrid:
    #     try:
    #         from sendgrid import SendGridAPIClient
    #         from sendgrid.helpers.mail import Mail
    #         sg = SendGridAPIClient(sg_api_key)
    #         last_message_id = ""
    #         for addr in recipients:
    #             message = Mail(
    #                 from_email=from_email,
    #                 to_emails=addr,
    #                 subject=final_subject,
    #                 html_content=html_content,
    #             )
    #             response = sg.send(message)
    #             last_message_id = response.headers.get("X-Message-Id", "")
    #             logger.info(f"[sendgrid] Email sent to {addr}, status={response.status_code}, id={last_message_id}")
    #         return {
    #             "success": True,
    #             "method": "sendgrid",
    #             "message_id": last_message_id,
    #             "to": recipients_str,
    #             "count": len(recipients),
    #             "is_test": is_test,
    #         }
    #     except Exception as e:
    #         logger.error(f"[sendgrid] Send failed: {e}")
    #         return {"success": False, "error": str(e)}


def list_resend_audiences() -> list:
    resend_api_key = os.environ.get("RESEND_API_KEY", "")
    if not resend_api_key:
        return []
    import resend
    resend.api_key = resend_api_key
    try:
        resp = resend.Audiences.list()
        if isinstance(resp, dict):
            return resp.get("data", [])
        elif isinstance(resp, list):
            return resp
        return [resp] if resp else []
    except Exception as e:
        logger.error(f"[resend] Failed to list audiences: {e}")
        return []


def list_resend_contacts(audience_id: str) -> list:
    resend_api_key = os.environ.get("RESEND_API_KEY", "")
    if not resend_api_key:
        return []
    import resend
    resend.api_key = resend_api_key
    try:
        resp = resend.Contacts.list(audience_id=audience_id)
        if isinstance(resp, dict):
            return resp.get("data", [])
        elif isinstance(resp, list):
            return resp
        return [resp] if resp else []
    except Exception as e:
        logger.error(f"[resend] Failed to list contacts for audience {audience_id}: {e}")
        return []


def unsubscribe_resend_contact(audience_id: str, email: str) -> dict:
    """Flip ``unsubscribed=True`` on a contact in a Resend audience.

    Returns a dict with ``success`` (bool) plus, on failure, ``error``
    (string) and ``status`` ("not_configured" | "not_found" |
    "update_failed"). The audience-fetch endpoint already filters out
    unsubscribed contacts, so flipping the flag is enough to keep the
    contact out of all future sends to this audience.
    """
    aud = (audience_id or "").strip()
    em = (email or "").strip().lower()
    if not aud or not em:
        return {"success": False, "status": "invalid_request", "error": "audience_id and email are required"}
    resend_api_key = os.environ.get("RESEND_API_KEY", "")
    if not resend_api_key:
        return {"success": False, "status": "not_configured", "error": "Resend is not configured (missing RESEND_API_KEY)"}
    import resend
    resend.api_key = resend_api_key
    try:
        # Resend's update-contact accepts either id or email; passing email
        # directly avoids a round-trip list call.
        resend.Contacts.update({
            "audience_id": aud,
            "email": em,
            "unsubscribed": True,
        })
        return {"success": True, "status": "ok"}
    except Exception as e:
        err_str = str(e).lower()
        if "not found" in err_str or "404" in err_str:
            return {"success": False, "status": "not_found", "error": "Contact not found in this audience"}
        logger.exception(
            f"[resend] Failed to unsubscribe contact: type={type(e).__name__} repr={e!r} "
            f"audience=<{aud[:8]}...> email=<{em[:3]}...>"
        )
        return {"success": False, "status": "update_failed", "error": str(e)}


def get_resend_template(template_id: str) -> dict:
    """Fetch a single Resend template's metadata and HTML body.

    Returns a dict with `success` plus, on success, `id`, `name`, `html`.
    On failure returns `success=False` and `error` (string).
    """
    if not template_id:
        return {"success": False, "error": "Template ID is required"}
    resend_api_key = os.environ.get("RESEND_API_KEY", "")
    if not resend_api_key:
        return {"success": False, "error": "Resend is not configured (missing RESEND_API_KEY)"}
    import resend
    resend.api_key = resend_api_key
    try:
        resp = resend.Templates.get(template_id)
        if isinstance(resp, dict):
            data = resp
        else:
            data = {
                "id": getattr(resp, "id", ""),
                "name": getattr(resp, "name", ""),
                "html": getattr(resp, "html", "") or getattr(resp, "content", ""),
            }
        html_body = data.get("html") or data.get("content") or ""
        return {
            "success": True,
            "id": data.get("id", "") or template_id,
            "name": data.get("name", "") or template_id,
            "html": html_body,
        }
    except Exception as e:
        logger.error(f"[resend] Failed to fetch template {template_id}: {e}")
        return {"success": False, "error": str(e), "id": template_id}


def list_resend_templates() -> list:
    resend_api_key = os.environ.get("RESEND_API_KEY", "")
    if not resend_api_key:
        return []
    import resend
    resend.api_key = resend_api_key
    try:
        resp = resend.Templates.list()
        if isinstance(resp, dict):
            return resp.get("data", [])
        elif isinstance(resp, list):
            return resp
        return [resp] if resp else []
    except Exception as e:
        logger.error(f"[resend] Failed to list templates: {e}")
        return []


# ---------------------------------------------------------------------------
# Resend Topics (per-topic unsubscribe — Task #77)
# ---------------------------------------------------------------------------
#
# Resend's Topics API stores subscription state at the workspace level
# (per email + topic), not per audience. That means an opt-out on
# "Product Update" applies to every audience that sends the same topic.
# We use it instead of custom contact properties because custom
# properties only work on global contacts, not on audience contacts.
#
# Helpers below are thin wrappers around the SDK so the route + send
# code never has to know about resend SDK internals or response shapes.


def _topic_to_dict(t) -> dict:
    """Normalize a Topic (dict or model) to a plain dict for JSON output."""
    if isinstance(t, dict):
        return {
            "id": t.get("id", "") or "",
            "name": t.get("name", "") or "",
            "description": t.get("description", "") or "",
            "default_subscription": (t.get("default_subscription") or "opt_in"),
        }
    return {
        "id": getattr(t, "id", "") or "",
        "name": getattr(t, "name", "") or "",
        "description": getattr(t, "description", "") or "",
        "default_subscription": getattr(t, "default_subscription", "opt_in") or "opt_in",
    }


def list_resend_topics() -> list:
    """Return all workspace topics as a list of plain dicts.

    Topics are managed in the Resend dashboard. The dashboard's topic
    picker calls this to populate its dropdown.
    """
    resend_api_key = os.environ.get("RESEND_API_KEY", "")
    if not resend_api_key:
        return []
    import resend
    resend.api_key = resend_api_key
    try:
        resp = resend.Topics.list()
        if isinstance(resp, dict):
            data = resp.get("data", []) or []
        elif isinstance(resp, list):
            data = resp
        else:
            data = list(getattr(resp, "data", []) or [])
        return [_topic_to_dict(t) for t in data]
    except Exception as e:
        logger.error(f"[resend] Failed to list topics: {e}")
        return []


def get_resend_topic(topic_id: str) -> dict | None:
    """Fetch a single topic by id, or ``None`` on failure.

    Used by the hosted unsubscribe page so we can show the topic's name
    in the confirmation message ("Confirm unsubscribe from Product
    Update emails").
    """
    tid = (topic_id or "").strip()
    if not tid:
        return None
    resend_api_key = os.environ.get("RESEND_API_KEY", "")
    if not resend_api_key:
        return None
    import resend
    resend.api_key = resend_api_key
    try:
        resp = resend.Topics.get(tid)
        return _topic_to_dict(resp) if resp else None
    except Exception as e:
        logger.warning(f"[resend] Failed to fetch topic <{tid[:8]}...>: {e}")
        return None


def _list_contact_topic_subs(email: str) -> tuple:
    """Return ``(subs, status)`` for a contact's topic subscriptions.

    ``status`` is one of:
      * ``"ok"``      — list returned (possibly empty); treat the
                        absence of an entry for a given topic as "no
                        explicit record, fall back to topic default".
      * ``"not_found"`` — contact has no global ContactsTopics record
                        yet (HTTP 404). Identical effect to ``ok`` with
                        an empty list.
      * ``"error"``   — Resend was reached but failed for another
                        reason, OR the SDK is missing/misconfigured. The
                        caller MUST fail-safe (do not assume
                        subscribed).
    """
    em = (email or "").strip().lower()
    if not em:
        return ([], "ok")
    resend_api_key = os.environ.get("RESEND_API_KEY", "")
    if not resend_api_key:
        # No API key means we cannot prove subscription state. Fail-safe
        # so we don't silently send to opted-out contacts after a config
        # rotation.
        return ([], "error")
    import resend
    resend.api_key = resend_api_key
    try:
        resp = resend.ContactsTopics.list(email=em)
        if isinstance(resp, dict):
            return (resp.get("data", []) or [], "ok")
        if isinstance(resp, list):
            return (resp, "ok")
        return (list(getattr(resp, "data", []) or []), "ok")
    except Exception as e:
        err_str = str(e).lower()
        if "not found" in err_str or "404" in err_str:
            return ([], "not_found")
        logger.warning(f"[resend] Failed to list contact topics for <{em[:3]}...>: {e}")
        return ([], "error")


def filter_emails_by_topic_subscription(emails: list, topic_id: str) -> dict:
    """Return effective opt-in recipients for ``topic_id``.

    Effective subscription resolution per recipient:
      1. If the contact has an explicit ContactTopic entry for
         ``topic_id``, use it (``opt_in`` / ``opt_out``).
      2. Otherwise fall back to the topic's ``default_subscription``.
      3. If the recipient's subscription state cannot be resolved
         reliably (Resend API error, missing key, or the topic itself
         could not be fetched), the recipient is reported as an error
         so the caller can fail-safe and abort the send. CAN-SPAM /
         compliance: when in doubt, do not send.

    Returns a dict::

        {
          "ok": bool,                # False if any errors OR topic
                                     # itself could not be fetched
          "kept": list[str],         # effective opt-ins
          "dropped_opt_out": int,    # explicitly opted out
          "errors": int,             # could not resolve (fail-safe)
          "default_subscription": str,  # "opt_in" | "opt_out" | ""
          "error": str | None,
        }

    v1 N+1 implementation: one ``ContactsTopics.list`` call per
    recipient. Acceptable at current audience sizes; can be batched
    later.
    """
    if not emails or not (topic_id or "").strip():
        return {
            "ok": True,
            "kept": list(emails or []),
            "dropped_opt_out": 0,
            "errors": 0,
            "default_subscription": "",
            "error": None,
        }
    tid = topic_id.strip()
    # Look up the topic so we know the default subscription. If we can't
    # fetch it, fail-safe: we don't know what to do with contacts that
    # have no explicit record, so refuse the whole send.
    topic = get_resend_topic(tid)
    if not topic:
        return {
            "ok": False,
            "kept": [],
            "dropped_opt_out": 0,
            "errors": len([e for e in emails if (e or "").strip()]),
            "default_subscription": "",
            "error": (
                "Could not look up the topic in Resend, so we cannot "
                "verify who is opted in. The send was blocked to avoid "
                "emailing people who may have unsubscribed."
            ),
        }
    default_sub = (topic.get("default_subscription") or "opt_in").lower()
    if default_sub not in ("opt_in", "opt_out"):
        default_sub = "opt_in"
    kept = []
    dropped = 0
    errors = 0
    # Resend allows ~5 contact-topic reads/sec. Space requests so a
    # mid-size audience doesn't get partially rate-limited and trip
    # the fail-safe abort on transient 429s.
    import time as _time_mod
    _MIN_INTERVAL_S = 0.22
    _last_call_at = 0.0
    for em in emails:
        em_clean = (em or "").strip()
        if not em_clean:
            continue
        _gap = _time_mod.monotonic() - _last_call_at
        if _gap < _MIN_INTERVAL_S:
            _time_mod.sleep(_MIN_INTERVAL_S - _gap)
        _last_call_at = _time_mod.monotonic()
        subs, status = _list_contact_topic_subs(em_clean)
        if status == "error":
            errors += 1
            # Fail-safe: do not send to a recipient whose subscription
            # we cannot prove. Counted as an error so the caller can
            # decide to abort.
            continue
        explicit_sub = ""
        for s in subs:
            sid = s.get("id") if isinstance(s, dict) else getattr(s, "id", "")
            sub_val = (s.get("subscription") if isinstance(s, dict) else getattr(s, "subscription", "")) or ""
            if (sid or "").strip() == tid:
                explicit_sub = sub_val.lower()
                break
        effective = explicit_sub if explicit_sub in ("opt_in", "opt_out") else default_sub
        if effective == "opt_out":
            dropped += 1
        else:
            kept.append(em_clean)
    if dropped or errors:
        logger.info(
            f"[resend] Topic filter: kept {len(kept)} opt-in, dropped {dropped} opt-out, "
            f"{errors} unresolved (default={default_sub})"
        )
    return {
        "ok": errors == 0,
        "kept": kept,
        "dropped_opt_out": dropped,
        "errors": errors,
        "default_subscription": default_sub,
        "error": (
            f"{errors} recipient(s) could not be resolved against Resend "
            "topics. The send was blocked to avoid emailing people who "
            "may have unsubscribed."
        ) if errors else None,
    }


def unsubscribe_resend_topic(email: str, topic_id: str) -> dict:
    """Set a contact's subscription for ``topic_id`` to ``opt_out``.

    Mirrors ``unsubscribe_resend_contact``'s return shape so the route
    can treat both the legacy (audience-wide) and new (per-topic)
    branches the same way: ``{"success": bool, "status": "ok" |
    "not_configured" | "not_found" | "invalid_request" |
    "update_failed", "error"?: str}``. The topics API works by email,
    so no audience-id round-trip is needed.
    """
    em = (email or "").strip().lower()
    tid = (topic_id or "").strip()
    if not em or not tid:
        return {"success": False, "status": "invalid_request", "error": "email and topic_id are required"}
    resend_api_key = os.environ.get("RESEND_API_KEY", "")
    if not resend_api_key:
        return {"success": False, "status": "not_configured", "error": "Resend is not configured (missing RESEND_API_KEY)"}
    import resend
    resend.api_key = resend_api_key
    try:
        resend.ContactsTopics.update({
            "email": em,
            "topics": [{"id": tid, "subscription": "opt_out"}],
        })
        return {"success": True, "status": "ok"}
    except Exception as e:
        err_str = str(e).lower()
        if "not found" in err_str or "404" in err_str:
            return {"success": False, "status": "not_found", "error": "Contact or topic not found"}
        logger.exception(
            f"[resend] Failed to opt out of topic: type={type(e).__name__} repr={e!r} "
            f"email=<{em[:3]}...> topic=<{tid[:8]}...>"
        )
        return {"success": False, "status": "update_failed", "error": str(e)}
