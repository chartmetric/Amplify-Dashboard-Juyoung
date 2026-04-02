import os
import io
import base64
import logging
import urllib.parse

logger = logging.getLogger(__name__)


def publish_tweet(content: str, image_base64: str = None) -> dict:
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
                    file_obj.name = "image.png"
                    media = api_v1.media_upload(filename="image.png", file=file_obj)
                    media_id = media.media_id
                    logger.info(f"[twitter] Image uploaded, media_id={media_id}")
                except Exception as img_err:
                    logger.error(f"[twitter] Image upload failed: {img_err}")
                    return {
                        "success": False,
                        "error": f"Image upload failed: {img_err}",
                        "error_type": "image_upload",
                    }

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

            error_type = "unknown"
            hint = ""
            if "403" in error_str:
                error_type = "forbidden"
                if "duplicate" in error_str.lower():
                    hint = "This tweet may be a duplicate. Try editing the text slightly before posting."
                else:
                    hint = "Your app may not have write permissions, or the tweet was rejected by X. Try editing the text and posting again."
            elif "401" in error_str:
                error_type = "auth"
                hint = "Authentication failed. Your API keys may be invalid or expired."
            elif "429" in error_str:
                error_type = "rate_limit"
                hint = "Rate limit reached. Wait a few minutes and try again."
            elif "402" in error_str:
                error_type = "payment"
                hint = "Your X developer account needs API credits. Check your X developer portal billing."

            return {
                "success": False,
                "error": error_str,
                "error_type": error_type,
                "hint": hint,
            }
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

        encoded = urllib.parse.quote(content, safe="")
        intent_url = f"https://twitter.com/intent/tweet?text={encoded}"
        result = {
            "success": True,
            "tweet_url": intent_url,
            "method": "fallback",
            "message": "Open this link to post the tweet",
            "missing_creds": missing,
        }
        if image_base64:
            result["message"] = (
                "Tweet text pre-filled. Paste your image manually in the compose box."
            )
            result["has_image_reminder"] = True
        return result
