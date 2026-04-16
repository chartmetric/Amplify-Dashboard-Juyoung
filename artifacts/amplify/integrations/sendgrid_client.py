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
    def _stash_link(m):
        link_text = _esc(m.group(1))
        url = m.group(2)
        if re.match(r'^https?://', url, re.IGNORECASE) or url.startswith('mailto:'):
            key = f'\x00LINK{counter[0]}\x00'
            counter[0] += 1
            placeholders[key] = f'<a href="{_esc(url)}" target="_blank" rel="noopener noreferrer" style="color:#00C9A7;text-decoration:underline;">{link_text}</a>'
            return key
        return m.group(1)
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', _stash_link, text)
    safe = _esc(text)
    safe = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', safe)
    safe = re.sub(r'\*(.+?)\*', r'<em>\1</em>', safe)
    for key, val in placeholders.items():
        safe = safe.replace(key, val)
    return safe


def _get_base_url() -> str:
    deploy_url = os.environ.get("REPLIT_DEPLOYMENT_URL", "")
    if deploy_url:
        return deploy_url.rstrip("/")
    dev_domain = os.environ.get("REPLIT_DEV_DOMAIN", "")
    if dev_domain:
        return f"https://{dev_domain}"
    return "http://localhost:5000"


def _build_hosted_image_map(images: dict) -> dict:
    if not images:
        return {}
    import re as _re
    base_url = _get_base_url()

    hosted = {}
    for img_name, data_url in images.items():
        if not data_url:
            continue
        if data_url.startswith("http"):
            hosted[img_name] = data_url
        elif data_url.startswith("data:image/"):
            from ai.publish_store import save_image as _save_img, IMAGES_DIR
            import uuid as _uuid, base64 as _b64, json as _json
            m = _re.match(r"data:image/(\w+);base64,(.+)", data_url)
            if not m:
                continue
            ext = m.group(1)
            img_id = _uuid.uuid4().hex[:12]
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
    lines = body.strip().split("\n")
    body_html = ""
    first_text_done = False
    for line in lines:
        stripped = line.strip()
        if not stripped:
            body_html += "<br>"
        elif not first_text_done and not re.match(r'^\[image:\s*(.+)\]$', stripped) and not re.match(r'^\[video:\s*(.+)\]$', stripped) and not stripped.startswith('#'):
            first_text_done = True
            body_html += f'<h2 style="margin:0 0 20px 0;color:#1a1d23;font-size:22px;font-weight:700;">{_inline_markdown(stripped)}</h2>'
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
            body_html += (
                f'<div style="text-align:center;margin:16px 0 20px 0;">'
                f'<video src="{_esc(vid_link)}" poster="{_esc(thumb_url)}" '
                f'controls preload="none" playsinline width="600" '
                f'style="display:block;max-width:100%;width:100%;height:auto;'
                f'border-radius:6px;margin:0 auto;background:#000;outline:none;border:0;">'
                f'<a href="{_esc(vid_link)}" target="_blank" rel="noopener noreferrer" style="text-decoration:none;display:block;">'
                f'<img src="{_esc(thumb_url)}" alt="Play video" '
                f'style="display:block;max-width:100%;height:auto;border-radius:6px;margin:0 auto;border:0;outline:none;">'
                f'</a>'
                f'</video>'
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
            first_text_done = True
            hm = re.match(r'^(#{1,3})\s+(.+)$', stripped)
            if hm:
                level = len(hm.group(1))
                sizes = {1: '24px', 2: '20px', 3: '17px'}
                body_html += f'<h{level} style="margin:0 0 12px 0;color:#1a1d23;font-size:{sizes[level]};font-weight:700;">{_inline_markdown(hm.group(2))}</h{level}>'
            else:
                body_html += f'<p style="margin:0 0 12px 0;color:#333333;font-size:15px;line-height:1.6;">{_inline_markdown(stripped)}</p>'
        elif stripped.startswith("- "):
            body_html += f'<li style="margin-bottom:6px;color:#333333;font-size:15px;line-height:1.6;">{_inline_markdown(stripped[2:])}</li>'
        else:
            cta_phrases = ["try it here", "check it out", "learn more", "get started", "see it in action", "explore now"]
            is_cta = any(p in stripped.lower() for p in cta_phrases)
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

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
<body style="margin:0;padding:0;background:#f4f4f7;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4f7;padding:24px 0;">
<tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;">
<tr><td style="background:#1a1d23;padding:20px 32px;border-radius:8px 8px 0 0;">
<span style="color:#ffffff;font-size:20px;font-weight:700;letter-spacing:-0.3px;">Chartmetric</span>
</td></tr>
<tr><td style="background:#ffffff;padding:32px;border-radius:0 0 8px 8px;">
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
            logger.error(f"[resend] Send failed: {e}")
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
    result = _send_via_resend(final_subject, html_content, recipients, is_test, from_name=from_name, template_id=template_id, bcc_emails=bcc_list)
    if result:
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
