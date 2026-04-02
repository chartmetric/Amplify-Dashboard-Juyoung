import os
import io
import re
import base64
import logging
import urllib.parse

logger = logging.getLogger(__name__)


def _strip_markdown_links(text: str) -> str:
    return re.sub(r'\[([^\]]*)\]\((https?://[^)]+)\)', r'\2', text)


def _make_fallback(content: str, image_base64: str = None, api_error: str = None) -> dict:
    encoded = urllib.parse.quote(content, safe="")
    intent_url = f"https://twitter.com/intent/tweet?text={encoded}"
    result = {
        "success": True,
        "tweet_url": intent_url,
        "method": "fallback",
        "message": "Open this link to post the tweet",
    }
    if api_error:
        result["api_error"] = api_error
    if image_base64:
        result["message"] = (
            "Tweet text pre-filled. Paste your image manually in the compose box."
        )
        result["has_image_reminder"] = True
    return result


def publish_tweet(content: str, image_base64: str = None) -> dict:
    content = _strip_markdown_links(content)

    api_key = os.environ.get("TWITTER_API_KEY")
    api_secret = os.environ.get("TWITTER_API_SECRET")
    access_token = os.environ.get("TWITTER_ACCESS_TOKEN")
    access_secret = os.environ.get("TWITTER_ACCESS_SECRET")

    has_creds = all([api_key, api_secret, access_token, access_secret])

    if has_creds:
        try:
            import tweepy

            auth = tweepy.OAuth1UserHandler(
                api_key, api_secret, access_token, access_secret
            )
            api_v1 = tweepy.API(auth)
            client = tweepy.Client(
                consumer_key=api_key,
                consumer_secret=api_secret,
                access_token=access_token,
                access_token_secret=access_secret,
            )

            media_id = None
            if image_base64:
                try:
                    image_data = base64.b64decode(image_base64)
                    file_obj = io.BytesIO(image_data)

                    if image_data[:8] == b'\x89PNG\r\n\x1a\n':
                        ext, mime = "png", "image/png"
                    elif image_data[:2] == b'\xff\xd8':
                        ext, mime = "jpg", "image/jpeg"
                    elif image_data[:4] == b'GIF8':
                        ext, mime = "gif", "image/gif"
                    elif image_data[:4] == b'RIFF' and image_data[8:12] == b'WEBP':
                        ext, mime = "webp", "image/webp"
                    else:
                        ext, mime = "png", "image/png"

                    filename = f"image.{ext}"
                    file_obj.name = filename
                    logger.info(f"[twitter] Uploading image: {len(image_data)} bytes, type={mime}")
                    media = api_v1.media_upload(
                        filename=filename,
                        file=file_obj,
                        media_category="tweet_image",
                    )
                    media_id = media.media_id
                    logger.info(f"[twitter] Image uploaded, media_id={media_id}")
                except Exception as img_err:
                    logger.error(f"[twitter] Image upload failed: {img_err}")
                    logger.info("[twitter] Falling back to intent URL after image upload failure")
                    return _make_fallback(content, image_base64, api_error=str(img_err))

            kwargs = {"text": content}
            if media_id:
                kwargs["media_ids"] = [media_id]

            logger.info(
                f"[twitter] Posting tweet ({len(content)} chars, image={'yes' if media_id else 'no'})"
            )
            response = client.create_tweet(**kwargs)
            tweet_id = response.data["id"]
            tweet_url = f"https://twitter.com/i/web/status/{tweet_id}"
            logger.info(f"[twitter] Tweet posted: {tweet_url}")
            return {
                "success": True,
                "tweet_id": str(tweet_id),
                "tweet_url": tweet_url,
                "method": "api",
                "has_image": bool(media_id),
            }
        except Exception as e:
            error_str = str(e)
            logger.error(f"[twitter] Tweet failed: {error_str}")

            if "403" in error_str or "401" in error_str:
                logger.info("[twitter] API rejected request, falling back to intent URL")
                return _make_fallback(content, image_base64, api_error=error_str)

            if "429" in error_str:
                return {
                    "success": False,
                    "error": error_str,
                    "error_type": "rate_limit",
                    "hint": "Rate limit reached. Wait a few minutes and try again.",
                }

            return _make_fallback(content, image_base64, api_error=error_str)
    else:
        missing = []
        if not api_key:
            missing.append("TWITTER_API_KEY")
        if not api_secret:
            missing.append("TWITTER_API_SECRET")
        if not access_token:
            missing.append("TWITTER_ACCESS_TOKEN")
        if not access_secret:
            missing.append("TWITTER_ACCESS_SECRET")
        logger.warning(f"[twitter] Missing credentials: {missing}")

        result = _make_fallback(content, image_base64)
        result["missing_creds"] = missing
        return result
