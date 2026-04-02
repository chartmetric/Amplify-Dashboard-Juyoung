import os
import urllib.parse


def publish_tweet(content: str) -> dict:
    api_key = os.environ.get("TWITTER_API_KEY")
    api_secret = os.environ.get("TWITTER_API_SECRET")
    access_token = os.environ.get("TWITTER_ACCESS_TOKEN")
    access_secret = os.environ.get("TWITTER_ACCESS_SECRET")

    has_creds = all([api_key, api_secret, access_token, access_secret])

    if has_creds:
        try:
            import tweepy

            client = tweepy.Client(
                consumer_key=api_key,
                consumer_secret=api_secret,
                access_token=access_token,
                access_token_secret=access_secret,
            )
            response = client.create_tweet(text=content)
            tweet_id = response.data["id"]
            tweet_url = f"https://twitter.com/i/web/status/{tweet_id}"
            return {
                "success": True,
                "tweet_id": str(tweet_id),
                "tweet_url": tweet_url,
                "method": "api",
            }
        except Exception as e:
            return {"success": False, "error": str(e)}
    else:
        encoded = urllib.parse.quote(content, safe="")
        intent_url = f"https://twitter.com/intent/tweet?text={encoded}"
        return {
            "success": True,
            "tweet_url": intent_url,
            "method": "fallback",
            "message": "Open this link to post the tweet",
        }
