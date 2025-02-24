import os
import json
import requests
import logging
import time
from flask import Flask, redirect, request, session, render_template, jsonify
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from google.auth.transport.requests import Request
import tweepy
from dotenv import load_dotenv  # Load environment variables from .env


# Load environment variables
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY")

# Allow HTTP for OAuth locally
os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

# Logging setup
logging.basicConfig(filename="youtube_api.log", level=logging.ERROR, format="%(asctime)s - %(levelname)s - %(message)s")

# Google OAuth 2.0 Config
CLIENT_SECRETS_FILE = os.getenv("YOUTUBE_CLIENT_SECRET")
SCOPES = [os.getenv("YOUTUBE_SCOPES")]
REDIRECT_URI = os.getenv("YOUTUBE_REDIRECT_URI")

# YouTube API URLs
YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"


# Twitter API Credentials (loaded from .env)
TWITTER_API_KEY = os.getenv("TWITTER_API_KEY")
TWITTER_API_SECRET = os.getenv("TWITTER_API_SECRET")
TWITTER_BEARER_TOKEN = os.getenv("TWITTER_BEARER_TOKEN")
TWITTER_ACCESS_TOKEN = os.getenv("TWITTER_ACCESS_TOKEN")
TWITTER_ACCESS_SECRET = os.getenv("TWITTER_ACCESS_SECRET")

# Securely retrieve PayPal Client ID
PAYPAL_CLIENT_ID = os.getenv("PAYPAL_CLIENT_ID")

# Use OAuth 1.0a authentication for Twitter API (required for fetching following list)
auth = tweepy.OAuth1UserHandler(
    consumer_key=TWITTER_API_KEY,
    consumer_secret=TWITTER_API_SECRET,
    access_token=TWITTER_ACCESS_TOKEN,
    access_token_secret=TWITTER_ACCESS_SECRET
)

# Create API client
api = tweepy.API(auth, wait_on_rate_limit=True)

def get_credentials():
    """Refresh and return credentials if expired"""
    if "credentials" not in session:
        return None

    credentials = Credentials(**session["credentials"])
    
    if credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(Request())
            session["credentials"] = credentials_to_dict(credentials)
        except Exception as e:
            logging.error(f"Failed to refresh token: {str(e)}")
            return None

    return credentials


@app.route("/")
def home():
    return render_template("index.html")  # ✅ Loads the new home page


@app.route("/choose_login")
def choose_login():
    """Page where users can choose between YouTube & Twitter login."""
    return render_template("choose_login.html")

@app.route("/youtube_ui")
def youtube_app():
    return render_template("youtube_ui.html", paypal_client_id=PAYPAL_CLIENT_ID)

@app.route("/twitter_ui")  # ✅ Fix: Add this route for Twitter UI
def twitter_app():
    return render_template("twitter_ui.html")  # Ensure this file exists in /templates


@app.route("/login")
def login():
    flow = Flow.from_client_secrets_file(
        CLIENT_SECRETS_FILE,
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI
    )
    auth_url, state = flow.authorization_url(prompt="consent")
    session["oauth_state"] = state
    return redirect(auth_url)


@app.route("/callback")
def callback():
    if "oauth_state" not in session:
        return jsonify({"error": "Session state missing. Try logging in again."}), 400

    stored_state = session.pop("oauth_state")  # Remove stored state
    received_state = request.args.get("state")

    if stored_state != received_state:
        return jsonify({"error": "CSRF Warning! State mismatch. Try again."}), 400

    flow = Flow.from_client_secrets_file(
        CLIENT_SECRETS_FILE,
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI,
        state=received_state
    )

    flow.fetch_token(authorization_response=request.url)
    credentials = flow.credentials
    session["credentials"] = credentials_to_dict(credentials)

    # ✅ Redirect to the YouTube dashboard after login
    return redirect("/youtube_ui")


@app.route("/my_subscriptions")
def get_my_subscriptions():
    credentials = get_credentials()
    if not credentials:
        return jsonify({"error": "User not authenticated"}), 401

    headers = {"Authorization": f"Bearer {credentials.token}"}

    response = requests.get(
        f"{YOUTUBE_API_BASE}/subscriptions",
        headers=headers,
        params={"part": "snippet", "mine": "true", "maxResults": 10000}
    )

    if response.status_code != 200:
        logging.error(f"Error retrieving user's subscriptions: {response.status_code}")
        return jsonify({"error": "Failed to retrieve subscriptions"}), 500

    data = response.json()
    
    my_subscriptions = {item["snippet"]["resourceId"]["channelId"] for item in data.get("items", [])}

    # 🔥 Debugging: Print subscriptions in terminal
    print(f"DEBUG - My Subscriptions: {my_subscriptions}")

    return jsonify({"my_subscriptions": list(my_subscriptions)})



@app.route("/subscriptions", methods=["GET"])
def get_subscriptions():
    credentials = get_credentials()
    if not credentials:
        return jsonify({"error": "User not authenticated"}), 401

    channel_id = request.args.get("channel_id")
    fetch_all = request.args.get("fetch_all") == "true"
    if not channel_id:
        return jsonify({"error": "Channel ID is required"}), 400

    headers = {"Authorization": f"Bearer {credentials.token}"}

    response = requests.get(
        f"{YOUTUBE_API_BASE}/subscriptions",
        headers=headers,
        params={"part": "snippet", "channelId": channel_id, "maxResults": 10000 if fetch_all else 4}
    )

    if response.status_code == 403:
        return jsonify({"error": "The channel's subscriptions are private or API quota exceeded."}), 403

    data = response.json()
    subscriptions = []

    for item in data.get("items", []):
        subscriptions.append({
            "title": item["snippet"]["title"],
            "channelId": item["snippet"]["resourceId"]["channelId"],
            "thumbnail": item["snippet"]["thumbnails"]["default"]["url"],
            "description": item["snippet"]["description"],
            "subscribers": 0
        })

    # Fetch subscriber counts separately in batches of 50
    for i in range(0, len(subscriptions), 50):
        batch_ids = ",".join(sub["channelId"] for sub in subscriptions[i:i+50])
        stats_response = requests.get(
            f"{YOUTUBE_API_BASE}/channels",
            headers=headers,
            params={"part": "statistics", "id": batch_ids}
        )

        if stats_response.status_code == 200:
            stats_data = stats_response.json().get("items", [])
            stats_map = {item["id"]: int(item["statistics"].get("subscriberCount", 0)) for item in stats_data}
            for sub in subscriptions[i:i+50]:
                sub["subscribers"] = stats_map.get(sub["channelId"], 0)
        else:
            logging.error(f"Failed fetching stats: {stats_response.text}")

    # Sort by subscriber count (highest first)
    subscriptions.sort(key=lambda x: x["subscribers"], reverse=True)

    channel_details = requests.get(
        f"{YOUTUBE_API_BASE}/channels",
        headers=headers,
        params={"part": "snippet", "id": channel_id}
    ).json()

    channel_name = channel_details["items"][0]["snippet"]["title"] if channel_details.get("items") else ""

    return jsonify({"subscriptions": subscriptions, "channel_name": channel_name})



@app.route("/subscribe", methods=["POST"])
def subscribe():
    """Subscribe to a channel"""
    credentials = get_credentials()
    if not credentials:
        return jsonify({"error": "User not authenticated"}), 401

    channel_id = request.json.get("channelId")
    if not channel_id:
        return jsonify({"error": "Channel ID is required"}), 400

    headers = {
        "Authorization": f"Bearer {credentials.token}",
        "Content-Type": "application/json"
    }
    data = {"snippet": {"resourceId": {"kind": "youtube#channel", "channelId": channel_id}}}

    response = requests.post(f"{YOUTUBE_API_BASE}/subscriptions?part=snippet", headers=headers, json=data)

    if response.status_code == 403:
        return jsonify({"error": "Cannot subscribe. User might have exceeded quota or subscriptions are restricted."}), 403

    return jsonify({"message": f"Successfully subscribed to {channel_id}!"})

@app.route("/batch_subscribe", methods=["POST"])
def batch_subscribe():
    credentials = get_credentials()
    if not credentials:
        return jsonify({"error": "User not authenticated"}), 401

    channel_ids = request.json.get("channelIds", [])
    if not channel_ids:
        return jsonify({"error": "No channels selected"}), 400

    headers = {
        "Authorization": f"Bearer {credentials.token}",
        "Content-Type": "application/json"
    }

    responses = []
    for channel_id in channel_ids:
        data = {"snippet": {"resourceId": {"kind": "youtube#channel", "channelId": channel_id}}}
        response = requests.post(f"{YOUTUBE_API_BASE}/subscriptions?part=snippet", headers=headers, json=data)
        responses.append(response.json())

    return jsonify({"message": "Subscription requests sent.", "responses": responses})

def credentials_to_dict(credentials):
    return {
        "token": credentials.token,
        "refresh_token": credentials.refresh_token,
        "token_uri": credentials.token_uri,
        "client_id": credentials.client_id,
        "client_secret": credentials.client_secret,
        "scopes": credentials.scopes
    }

#Twitter

@app.route("/twitter_callback")
def twitter_callback():
    # Placeholder for Twitter OAuth handling
    # ✅ Redirect user to Twitter UI after login
    return redirect("/twitter_ui")

@app.route("/twitter_following", methods=["GET"])
def get_twitter_following():
    username = request.args.get("username")
    if not username:
        return jsonify({"error": "Username is required"}), 400

    try:
        # Fetch user details
        user = api.get_user(screen_name=username)
        user_id = user.id_str

        # Fetch the list of accounts this user is following
        following = api.get_friends(user_id=user_id, count=10000)  # Adjust count if needed

        accounts = []
        for account in following:
            accounts.append({
                "id": account.id_str,
                "name": account.name,
                "username": account.screen_name,
                "profile_image": account.profile_image_url_https,
                "description": account.description,
                "followers": account.followers_count
            })

        return jsonify({"following": accounts})


    except tweepy.errors.Unauthorized as e:
        print(f"❌ Twitter API Unauthorized Error: {e}")
        return jsonify({"error": "Twitter API Authentication failed. Check your credentials."}), 401

    except tweepy.errors.TweepyException as e:
        print(f"❌ Tweepy API Error: {e}")
        return jsonify({"error": f"Twitter API error: {str(e)}"}), 500

    except Exception as e:
        print(f"❌ Unexpected error: {e}")
        return jsonify({"error": f"Unexpected error: {str(e)}"}), 500
    
@app.route("/twitter_follow", methods=["POST"])
def follow_twitter_account():
    """Follow an account (OAuth required)."""
    return jsonify({"message": "Following users requires OAuth authentication"}), 400

# Track Twitter clicks (simple in-memory count)
twitter_clicks = 0

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")  # Redirect to homepage

@app.route("/login_twitter")
def login_twitter():
    global twitter_clicks
    twitter_clicks += 1  # increment click count
    return render_template("twitter_coming_soon.html")

@app.route("/twitter_clicks")
def get_twitter_clicks():
    return jsonify({"twitter_clicks": twitter_clicks})

if __name__ == "__main__":
    app.run(debug=True)
