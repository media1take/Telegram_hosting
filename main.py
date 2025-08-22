from flask import Flask, jsonify, request
from telethon import TelegramClient
import os

# ========================
# Telegram API credentials
# ========================
API_ID = "YOUR_API_ID"        # Get from https://my.telegram.org
API_HASH = "YOUR_API_HASH"
CHANNEL_USERNAME = "your_channel_username_or_id"  # can be @channel or channel_id

# Flask app
app = Flask(__name__)

# Initialize Telegram client (session file will be saved)
client = TelegramClient("session", API_ID, API_HASH)

@app.before_first_request
def init_telegram():
    client.start()  # auto-login (first time it will ask for OTP)

# ========================
# Fetch all videos
# ========================
@app.route("/videos", methods=["GET"])
async def get_videos():
    videos = []
    async for message in client.iter_messages(CHANNEL_USERNAME):
        if message.video:  # Only videos
            videos.append({
                "id": message.id,
                "title": message.file.name if message.file else f"Video {message.id}",
                "file_id": message.video.id,
                "thumbnail": message.video.thumbs[0].location.__dict__ if message.video.thumbs else None
            })
    return jsonify(videos)

# ========================
# Get direct video URL
# ========================
@app.route("/video/<int:msg_id>", methods=["GET"])
async def get_video_url(msg_id):
    message = await client.get_messages(CHANNEL_USERNAME, ids=msg_id)
    if not message or not message.video:
        return jsonify({"error": "Video not found"}), 404
    
    # Generate download/stream URL
    file = await client.download_media(message, file=bytes)  # fetch as bytes
    return jsonify({
        "id": message.id,
        "title": message.file.name if message.file else f"Video {message.id}",
        "size": message.file.size,
        "direct_url": f"https://api.telegram.org/file/bot<YOUR_BOT_TOKEN>/{message.video}"
    })

# ========================
# Run Flask
# ========================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
