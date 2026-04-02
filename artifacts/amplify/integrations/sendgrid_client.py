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


def render_email_html(subject: str, body: str, images: dict = None) -> str:
    import re
    safe_subject = _esc(subject)
    image_map = images or {}
    lines = body.strip().split("\n")
    body_html = ""
    for line in lines:
        stripped = line.strip()
        if not stripped:
            body_html += "<br>"
        elif re.match(r'^\[image:\s*(.+)\]$', stripped):
            img_name = re.match(r'^\[image:\s*(.+)\]$', stripped).group(1).strip()
            img_src = image_map.get(img_name)
            if img_src:
                body_html += f'<div style="margin:16px 0;"><img src="{_esc(img_src)}" alt="{_esc(img_name)}" style="max-width:100%;height:auto;border-radius:6px;display:block;"></div>'
            else:
                body_html += f'<p style="margin:0 0 12px 0;color:#999999;font-size:13px;font-style:italic;">[Image: {_esc(img_name)}]</p>'
        elif re.match(r'^#{1,3}\s+', stripped):
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
                    rest = stripped[:md_link.start()] + stripped[md_link.end():]
                    label = _esc(rest.strip().rstrip('.').rstrip(':').strip() or md_link.group(1))
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
<h2 style="margin:0 0 20px 0;color:#1a1d23;font-size:22px;font-weight:700;">{safe_subject}</h2>
{body_html}
<hr style="border:none;border-top:1px solid #e8e8eb;margin:28px 0 16px 0;">
<p style="margin:0;color:#999999;font-size:12px;">Chartmetric &middot; Product Update</p>
</td></tr>
</table>
</td></tr>
</table>
</body>
</html>"""


def send_email(subject: str, body: str, to_email: str = None, is_test: bool = True, images: dict = None) -> dict:
    api_key = os.environ.get("SENDGRID_API_KEY")
    from_email = os.environ.get("SENDGRID_FROM_EMAIL")
    test_email = os.environ.get("SENDGRID_TEST_EMAIL")

    if not api_key or not from_email:
        logger.warning("[sendgrid] Missing SENDGRID_API_KEY or SENDGRID_FROM_EMAIL")
        preview_html = render_email_html(subject, body, images=images)
        return {
            "success": True,
            "method": "fallback",
            "message": "Email draft ready. SendGrid not configured.",
            "subject": subject,
            "body": body,
            "preview_html": preview_html,
        }

    recipient = to_email
    if not recipient:
        recipient = test_email if is_test else None
    if not recipient:
        return {
            "success": False,
            "error": "No recipient email provided. Set SENDGRID_TEST_EMAIL or provide to_email.",
        }

    final_subject = f"[TEST] {subject}" if is_test else subject
    html_content = render_email_html(final_subject, body, images=images)

    try:
        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import Mail

        message = Mail(
            from_email=from_email,
            to_emails=recipient,
            subject=final_subject,
            html_content=html_content,
        )
        sg = SendGridAPIClient(api_key)
        response = sg.send(message)
        message_id = response.headers.get("X-Message-Id", "")
        logger.info(f"[sendgrid] Email sent to {recipient}, status={response.status_code}, id={message_id}")
        return {
            "success": True,
            "method": "sendgrid",
            "message_id": message_id,
            "to": recipient,
            "is_test": is_test,
            "status_code": response.status_code,
        }
    except Exception as e:
        logger.error(f"[sendgrid] Send failed: {e}")
        return {
            "success": False,
            "error": str(e),
        }
