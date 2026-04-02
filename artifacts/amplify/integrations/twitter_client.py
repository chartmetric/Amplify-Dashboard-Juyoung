import os
import io
import base64
import urllib.parse


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
                image_data = base64.b64decode(image_base64)
                file_obj = io.BytesIO(image_data)
                file_obj.name = "image.png"
                media = api_v1.media_upload(filename="image.png", file=file_obj)
                media_id = media.media_id

            kwargs = {"text": content}
            if media_id:
                kwargs["media_ids"] = [media_id]

            response = client.create_tweet(**kwargs)
            tweet_id = response.data["id"]
            tweet_url = f"https://twitter.com/i/web/status/{tweet_id}"
            return {
                "success": True,
                "tweet_id": str(tweet_id),
                "tweet_url": tweet_url,
                "method": "api",
                "has_image": bool(media_id),
            }
        except Exception as e:
            return {"success": False, "error": str(e)}
    else:
        encoded = urllib.parse.quote(content, safe="")
        intent_url = f"https://twitter.com/intent/tweet?text={encoded}"
        result = {
            "success": True,
            "tweet_url": intent_url,
            "method": "fallback",
            "message": "Open this link to post the tweet",
        }
        if image_base64:
            result["message"] = (
                "Tweet text pre-filled. Paste your image manually in the compose box."
            )
            result["has_image_reminder"] = True
        return result
