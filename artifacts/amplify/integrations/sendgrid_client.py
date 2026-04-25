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
    deploy_url = os.environ.get("REPLIT_DEPLOYMENT_URL", "")
    if deploy_url:
        return deploy_url.rstrip("/")
    dev_domain = os.environ.get("REPLIT_DEV_DOMAIN", "")
    if dev_domain:
        return f"https://{dev_domain}"
    return "http://localhost:5000"


def _build_hosted_image_map(images: dict) -> dict:
    """Persist inline data: images so the email can reference a stable URL.

    Tries Postgres first (durable across deploys); falls back to the on-disk
    cache used by the legacy ``/api/publish/image/hosted/<id>`` route. The
    legacy disk cache gets wiped on every Replit redeploy, which silently
    broke images in already-sent emails — Postgres avoids that.
    """
    if not images:
        return {}
    import re as _re
    base_url = _get_base_url()

    try:
        from app import save_hosted_image_db as _save_db  # type: ignore
    except Exception:
        _save_db = None

    hosted = {}
    for img_name, data_url in images.items():
        if not data_url:
            continue
        if data_url.startswith("http"):
            hosted[img_name] = data_url
        elif data_url.startswith("data:image/"):
            import uuid as _uuid, base64 as _b64, json as _json
            m = _re.match(r"data:image/(\w+);base64,(.+)", data_url)
            if not m:
                continue
            ext = m.group(1)
            img_id = _uuid.uuid4().hex[:12]
            try:
                raw = _b64.b64decode(m.group(2))
            except Exception:
                logger.warning(f"[email] Skipping image '{img_name}': invalid base64 data")
                continue
            stored_in_db = False
            if _save_db is not None:
                try:
                    stored_in_db = bool(_save_db(img_id, ext, str(img_name)[:200], raw))
                except Exception as e:
                    logger.warning(f"[email] DB hosted-image save failed for '{img_name}': {e}")
            if not stored_in_db:
                # Fall back to disk so local dev / unconfigured envs still work.
                from ai.publish_store import IMAGES_DIR
                img_dir = os.path.join(IMAGES_DIR, f"_hosted_{img_id}")
                os.makedirs(img_dir, exist_ok=True)
                with open(os.path.join(img_dir, "image.dat"), "w") as f:
                    f.write(data_url)
                with open(os.path.join(img_dir, "meta.json"), "w") as f:
                    _json.dump({"name": str(img_name)[:200], "ext": ext, "id": img_id}, f)
            hosted[img_name] = f"{base_url}/api/publish/image/hosted/{img_id}"
        else:
            hosted[img_name] = data_url
    return hosted


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
    return 'https://via.placeholder.com/480x270/e0e0e0/999999?text=Video'


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


def render_email_html(subject: str, body: str, images: dict = None, cid_map: dict = None, from_name: str = None, videos: dict = None) -> str:
    _ = from_name
    import re
    safe_subject = _esc(subject)
    image_map = images or {}
    video_map = videos or {}

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
        elif re.match(r'^\[video:\s*(.+)\]$', stripped):
            vid_ref = re.match(r'^\[video:\s*(.+)\]$', stripped).group(1).strip()
            if re.match(r'^https?://', vid_ref, re.IGNORECASE):
                remote_thumb = _get_video_thumbnail(vid_ref)
                thumb_url = _composited_external_thumb_url(remote_thumb)
                vid_link = vid_ref
            elif vid_ref in video_map:
                vid_info = video_map[vid_ref]
                thumb_url = vid_info.get("thumb_url", "")
                vid_link = vid_info.get("video_url", "")
            else:
                body_html += f'<p style="margin:0 0 12px 0;color:#999999;font-size:13px;font-style:italic;">[Video: {_esc(vid_ref)}]</p>'
                continue
            if not thumb_url or not vid_link:
                body_html += f'<p style="margin:0 0 12px 0;color:#999999;font-size:13px;font-style:italic;">[Video: {_esc(vid_ref)}]</p>'
                continue
            esc_link = _esc(vid_link)
            esc_thumb = _esc(thumb_url)
            body_html += (
                f'<div style="text-align:center;margin:16px 0 20px 0;">'
                f'<div style="position:relative;display:inline-block;max-width:600px;width:100%;margin:0 auto;line-height:0;">'
                f'<a href="{esc_link}" target="_blank" rel="noopener noreferrer" '
                f'style="display:block;text-decoration:none;">'
                f'<img src="{esc_thumb}" alt="Play video" width="600" '
                f'style="display:block;width:100%;max-width:600px;height:auto;border-radius:6px;border:0;outline:none;">'
                f'</a>'
                f'<video src="{esc_link}" poster="{esc_thumb}" '
                f'controls preload="none" playsinline '
                f'style="position:absolute;top:0;left:0;width:100%;height:100%;border-radius:6px;background:#000;outline:none;border:0;">'
                f'</video>'
                f'</div>'
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
                body_html += f'<p style="margin:0 0 12px 0;color:#999999;font-size:13px;font-style:italic;">[Image: {_esc(img_name)}]</p>'
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
            cta_phrases = ["try it here", "try it now", "check it out", "learn more", "get started", "see it in action", "see the chart", "see it now", "explore now", "explore", "see how"]
            standalone_link = re.match(r'^\[([^\]]+)\]\((https?://[^)]+)\)\s*\.?$', stripped)
            is_cta = standalone_link is not None or any(p in stripped.lower() for p in cta_phrases)
            if is_cta and ("http" in stripped):
                md_link = re.search(r'\[([^\]]+)\]\((https?://[^)]+)\)', stripped)
                bare_url = re.search(r'(https?://\S+)', stripped)
                if md_link:
                    url = _esc(md_link.group(2))
                    link_text = md_link.group(1).strip()
                    rest = (stripped[:md_link.start()] + stripped[md_link.end():]).strip().rstrip('.').rstrip(':').strip()
                    label = _esc(link_text or rest or "Try it now")
                    if rest:
                        body_html += f'<p style="margin:0 0 4px 0;color:#333333;font-size:15px;line-height:1.6;">{_inline_markdown(rest)}</p>'
                    body_html += f'<div style="text-align:center;margin:24px 0;"><a href="{url}" style="display:inline-block;background:#00C9A7;color:#ffffff;text-decoration:none;padding:12px 32px;border-radius:6px;font-weight:700;font-size:15px;">{label}</a></div>'
                elif bare_url:
                    url = _esc(bare_url.group(1).rstrip(').,;'))
                    label = _esc(re.sub(r'https?://\S+', '', stripped).strip().rstrip(':').strip() or "Try it now")
                    body_html += f'<div style="text-align:center;margin:24px 0;"><a href="{url}" style="display:inline-block;background:#00C9A7;color:#ffffff;text-decoration:none;padding:12px 32px;border-radius:6px;font-weight:700;font-size:15px;">{label}</a></div>'
                else:
                    body_html += f'<p style="margin:0 0 12px 0;color:#333333;font-size:15px;line-height:1.6;">{_inline_markdown(stripped)}</p>'
            else:
                body_html += f'<p style="margin:0 0 12px 0;color:#333333;font-size:15px;line-height:1.6;">{_inline_markdown(stripped)}</p>'

    close_list()
    if pending_chip_html:
        body_html += pending_chip_html

    safe_banner_title = _esc(banner_title)
    safe_banner_month = _esc(banner_month)

    banner_row = f"""<tr><td style="background:linear-gradient(135deg,#0f172a 0%,#1a1d23 60%,#0b3b33 100%);padding:32px;border-bottom:3px solid #00C9A7;">
<div style="color:#9ae6d4;font-size:11px;font-weight:700;letter-spacing:1.2px;text-transform:uppercase;margin-bottom:6px;">{safe_banner_month}</div>
<div style="color:#ffffff;font-size:26px;font-weight:800;letter-spacing:-0.5px;line-height:1.2;">{safe_banner_title}</div>
</td></tr>""" if has_banner else ""
    header_radius = "8px 8px 0 0" if has_banner else "8px 8px 0 0"
    body_radius = "0 0 8px 8px"

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
<body style="margin:0;padding:0;background:#f4f4f7;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4f7;padding:24px 0;">
<tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;">
<tr><td style="background:#1a1d23;padding:18px 32px;border-radius:{header_radius};">
<span style="color:#ffffff;font-size:20px;font-weight:700;letter-spacing:-0.3px;">Chartmetric</span>
</td></tr>
{banner_row}
<tr><td style="background:#ffffff;padding:32px;border-radius:{body_radius};">
{body_html}
<hr style="border:none;border-top:1px solid #e8e8eb;margin:28px 0 16px 0;">
<p style="margin:0;color:#999999;font-size:12px;">Chartmetric &middot; Product Update</p>
</td></tr>
</table>
</td></tr>
</table>
</body>
</html>"""


def _send_via_resend(subject: str, html_content: str, to_emails: list, is_test: bool, attachments: list = None, from_name: str = None, template_id: str = None, bcc_emails: list = None) -> dict | None:
    import time as _time
    resend_api_key = os.environ.get("RESEND_API_KEY", "")
    from_email = os.environ.get("RESEND_FROM_EMAIL", "") or os.environ.get("SENDGRID_FROM_EMAIL", "")
    if not resend_api_key or not from_email:
        return None

    import resend
    resend.api_key = resend_api_key

    display_name = from_name or "Chartmetric"
    sender = f"{display_name} <{from_email}>"
    email_list = []
    for addr in to_emails:
        params = {
            "from": sender,
            "to": [addr],
            "subject": subject,
        }
        if bcc_emails:
            params["bcc"] = bcc_emails
        if template_id:
            params["template"] = {"id": template_id}
        else:
            params["html"] = html_content
        if attachments:
            params["attachments"] = attachments
        email_list.append(params)

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
            logger.info(f"[resend] Sent {len(to_emails)} individual email(s) to {to_str}, ids={ids}")
            return {
                "success": True,
                "method": "resend",
                "message_id": ids[0] if ids else "",
                "to": to_str,
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


def send_email(subject: str, body: str, to_email: str = None, is_test: bool = True, images: dict = None, from_name: str = None, template_id: str = None, videos: dict = None, bcc_email: str = None) -> dict:
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

    hosted_images = _build_hosted_image_map(images)
    bcc_list = [e.strip() for e in (bcc_email or "").split(",") if e.strip()] if bcc_email else None
    html_content = render_email_html(final_subject, body, images=hosted_images, from_name=from_name, videos=videos)
    if videos:
        video_attachments, skipped_videos = _build_video_attachments(videos)
    else:
        video_attachments, skipped_videos = [], []
    result = _send_via_resend(final_subject, html_content, recipients, is_test, attachments=video_attachments or None, from_name=from_name, template_id=template_id, bcc_emails=bcc_list)
    if result:
        if skipped_videos:
            result["skipped_videos"] = skipped_videos
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
