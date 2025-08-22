# main.py
from fastapi import FastAPI, HTTPException, Request, Response, Query
from fastapi.responses import StreamingResponse, PlainTextResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from telethon import TelegramClient
from typing import Dict, Any, List, Optional
from datetime import datetime
import asyncio
import io
import os

# ========================
# Telegram API credentials
# ========================
API_ID = 11468953
API_HASH = "99f7513ef4889752f6278af3286a929c"

# ========================
# Channels (edit these!)
# ========================
CHANNELS: Dict[str, int] = {
    "movies": -1002530324145,
    # "music":  -1001234567890,
    # "sports": -1002222222222,
}

# ========================
# App & Client
# ========================
app = FastAPI(title="TG Video Hub", version="2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # <-- set your website origin(s) in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

client = TelegramClient("session", API_ID, API_HASH)

# Small in-memory caches (restart-safe alternative = Redis)
THUMB_CACHE: Dict[str, bytes] = {}
META_CACHE: Dict[str, Dict[str, Any]] = {}

# ========================
# Startup / Shutdown
# ========================
@app.on_event("startup")
async def startup_event():
    await client.start()
    print("Telegram client connected ✅")

@app.on_event("shutdown")
async def shutdown_event():
    await client.disconnect()
    print("Telegram client disconnected ❌")

# ========================
# Helpers
# ========================
def _resolve_channel_id(channel: str) -> int:
    if channel not in CHANNELS:
        raise HTTPException(status_code=404, detail=f"Channel '{channel}' not found")
    return CHANNELS[channel]

def _is_video_msg(msg) -> bool:
    # Video can be a proper .video or a document with video/* mime
    try:
        if msg.video:
            return True
    except Exception:
        pass
    try:
        if msg.document and msg.document.mime_type and msg.document.mime_type.startswith("video"):
            return True
    except Exception:
        pass
    return False

def _video_meta(msg) -> Dict[str, Any]:
    key = f"{msg.chat_id}:{msg.id}"
    cached = META_CACHE.get(key)
    if cached:
        return cached

    f = getattr(msg, "file", None)
    v = getattr(msg, "video", None)
    meta = {
        "id": msg.id,
        "title": (f.name if f and getattr(f, "name", None) else f"Video {msg.id}"),
        "size": (f.size if f and getattr(f, "size", None) is not None else None),
        "duration": (getattr(v, "duration", None) if v else None),
        "mime_type": (f.mime_type if f and getattr(f, "mime_type", None) else None),
        "thumbnail_exists": bool(getattr(v, "thumbs", None)) if v else False,
        "date": (msg.date.isoformat() if isinstance(msg.date, datetime) else str(msg.date)),
    }
    META_CACHE[key] = meta
    return meta

async def _read_range(message, start: int, length: Optional[int]) -> bytes:
    """
    Fetch a byte range from Telegram. Uses Telethon download_file with offset/limit in BYTES.
    """
    data = await client.download_file(
        message.media,
        file=bytes,
        offset=start if start else 0,
        limit=length if length is not None else None
    )
    # Telethon may return less than requested if near EOF; caller handles this.
    return data

# ========================
# Health / Channels / Stats
# ========================
@app.get("/health")
async def health():
    return {"ok": True, "channels": list(CHANNELS.keys())}

@app.get("/channels")
async def get_channels():
    return {"channels": list(CHANNELS.keys())}

@app.get("/stats")
async def stats():
    """
    Lightweight stats (scans a few recent messages per channel).
    For heavy stats, build an index/store.
    """
    out = {}
    for name, cid in CHANNELS.items():
        total_scanned = 0
        vids = 0
        async for m in client.iter_messages(cid, limit=200):
            total_scanned += 1
            if _is_video_msg(m):
                vids += 1
        out[name] = {"recent_scanned": total_scanned, "videos_found": vids}
    return out

# ========================
# List / Recent / Swipe
# ========================
@app.get("/videos")
async def get_videos(
    channel: str = "movies",
    limit: int = 20,
    offset_id: int = 0
):
    """
    List videos in a channel (paginated). Use offset_id to continue scrolling.
    """
    channel_id = _resolve_channel_id(channel)
    videos: List[Dict[str, Any]] = []

    async for message in client.iter_messages(channel_id, limit=limit, offset_id=offset_id):
        if _is_video_msg(message):
            videos.append(_video_meta(message))

    next_offset_id = videos[-1]["id"] if videos else offset_id
    return {"channel": channel, "videos": videos, "next_offset_id": next_offset_id}

@app.get("/recent")
async def recent_across_channels(limit_per_channel: int = 10):
    """
    Collect recent videos across all channels (up to N per channel).
    """
    results = []
    for name, cid in CHANNELS.items():
        count = 0
        async for message in client.iter_messages(cid, limit=100):
            if _is_video_msg(message):
                m = _video_meta(message)
                m["channel"] = name
                results.append(m)
                count += 1
                if count >= limit_per_channel:
                    break
    results.sort(key=lambda x: x["date"], reverse=True)
    return {"results": results}

@app.get("/swipe")
async def swipe_neighbors(msg_id: int, channel: str = "movies"):
    """
    Get prev/next neighbor video IDs for swipe UX.
    """
    channel_id = _resolve_channel_id(channel)
    prev_id = None
    next_id = None

    # previous (older): id < current
    async for m in client.iter_messages(channel_id, offset_id=msg_id, reverse=True, limit=50):
        if _is_video_msg(m) and m.id < msg_id:
            prev_id = m.id
            break

    # next (newer): id > current
    async for m in client.iter_messages(channel_id, min_id=msg_id, limit=50):
        if _is_video_msg(m) and m.id > msg_id:
            next_id = m.id
            break

    return {"channel": channel, "current": msg_id, "prev": prev_id, "next": next_id}

# ========================
# Search (single / multi)
# ========================
@app.get("/search")
async def search_videos(
    query: str,
    channel: str = "movies",
    limit: int = 30
):
    """
    Search by filename in a specific channel.
    """
    channel_id = _resolve_channel_id(channel)
    results = []
    async for message in client.iter_messages(channel_id, limit=700):
        if _is_video_msg(message):
            name = (message.file.name if getattr(message, "file", None) else "") or ""
            if query.lower() in name.lower():
                results.append(_video_meta(message))
                if len(results) >= limit:
                    break
    return {"channel": channel, "query": query, "results": results}

@app.get("/search_all")
async def search_all(
    query: str,
    channels: Optional[str] = Query(None, description="Comma-separated channel keys"),
    limit_per_channel: int = 20
):
    """
    Search across ALL (or specified) channels.
    ?channels=movies,music   (optional)
    """
    targets = CHANNELS
    if channels:
        names = [c.strip() for c in channels.split(",") if c.strip()]
        targets = {n: CHANNELS[n] for n in names if n in CHANNELS}
        if not targets:
            raise HTTPException(status_code=404, detail="No valid channels provided")

    out = []
    for name, cid in targets.items():
        count = 0
        async for message in client.iter_messages(cid, limit=1000):
            if _is_video_msg(message):
                namefile = (message.file.name if getattr(message, "file", None) else "") or ""
                if query.lower() in namefile.lower():
                    m = _video_meta(message)
                    m["channel"] = name
                    out.append(m)
                    count += 1
                    if count >= limit_per_channel:
                        break
    out.sort(key=lambda x: x["date"], reverse=True)
    return {"query": query, "results": out}

# ========================
# Metadata / Thumbnail
# ========================
@app.get("/video/{msg_id}")
async def get_video(msg_id: int, channel: str = "movies"):
    channel_id = _resolve_channel_id(channel)
    message = await client.get_messages(channel_id, ids=msg_id)
    if not message or not _is_video_msg(message):
        raise HTTPException(status_code=404, detail="Video not found")

    meta = _video_meta(message)
    meta.update({
        "channel": channel,
        "direct_url": f"/stream/{msg_id}?channel={channel}",
        "download_url": f"/download/{msg_id}?channel={channel}",
        "thumbnail_url": f"/thumbnail/{msg_id}?channel={channel}",
    })
    return meta

@app.get("/thumbnail/{msg_id}")
async def get_thumbnail(msg_id: int, channel: str = "movies"):
    channel_id = _resolve_channel_id(channel)
    cache_key = f"{channel_id}:{msg_id}"
    if cache_key in THUMB_CACHE:
        return StreamingResponse(io.BytesIO(THUMB_CACHE[cache_key]), media_type="image/jpeg")

    message = await client.get_messages(channel_id, ids=msg_id)
    if not message or not _is_video_msg(message):
        raise HTTPException(status_code=404, detail="Video not found")

    thumbs = getattr(getattr(message, "video", None), "thumbs", None)
    if not thumbs:
        raise HTTPException(status_code=404, detail="Thumbnail not available")

    thumb = thumbs[0]  # choose smallest
    blob = await client.download_media(thumb, file=bytes)
    THUMB_CACHE[cache_key] = blob
    return StreamingResponse(io.BytesIO(blob), media_type="image/jpeg")

# ========================
# Streaming (Range) & Download
# ========================
@app.get("/stream/{msg_id}")
async def stream_video(request: Request, msg_id: int, channel: str = "movies"):
    """
    Range-capable endpoint for <video> tag (supports instant seek/forward/back).
    """
    channel_id = _resolve_channel_id(channel)
    message = await client.get_messages(channel_id, ids=msg_id)
    if not message or not _is_video_msg(message):
        raise HTTPException(status_code=404, detail="Video not found")

    f = getattr(message, "file", None)
    total = getattr(f, "size", None)
    if total is None:
        raise HTTPException(status_code=500, detail="Unknown file size")

    mime = getattr(f, "mime_type", None) or "video/mp4"
    range_header = request.headers.get("range", None)

    if range_header:
        try:
            # "bytes=START-END"
            _, rng = range_header.split("=")
            start_s, end_s = (rng.split("-") + [None])[:2]
            start = int(start_s) if start_s else 0
            # cap range window to ~2MB to keep latency low; browser will request more
            default_end = min(start + 2 * 1024 * 1024 - 1, total - 1)
            end = int(end_s) if end_s else default_end
            if end >= total:
                end = total - 1
            length = end - start + 1

            # Fetch that range from Telegram
            chunk = await _read_range(message, start, length)
            # Telethon can return fewer bytes at EOF; compute actual end
            actual_end = start + len(chunk) - 1

            headers = {
                "Content-Range": f"bytes {start}-{actual_end}/{total}",
                "Accept-Ranges": "bytes",
                "Content-Length": str(len(chunk)),
                "Content-Type": mime,
                "Cache-Control": "no-store",
            }
            return Response(content=chunk, status_code=206, headers=headers)
        except Exception as e:
            # Fall back to full streaming if Range parse fails
            print("⚠️ Range handling error:", e)

    # Progressive full stream (no Range)
    async def full_iter():
        # Larger chunks reduce round-trips; tune for your network
        async for chunk in client.iter_download(message.media, chunk_size=2 * 1024 * 1024):
            yield chunk

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(total),
        "Content-Type": mime,
        "Cache-Control": "no-store",
    }
    return StreamingResponse(full_iter(), headers=headers, media_type=mime)

@app.get("/download/{msg_id}")
async def download_video(msg_id: int, channel: str = "movies"):
    """
    Force download (no Range). Browser will save the file.
    """
    channel_id = _resolve_channel_id(channel)
    message = await client.get_messages(channel_id, ids=msg_id)
    if not message or not _is_video_msg(message):
        raise HTTPException(status_code=404, detail="Video not found")

    f = getattr(message, "file", None)
    filename = (getattr(f, "name", None) or f"video_{msg_id}.mp4")
    mime = (getattr(f, "mime_type", None) or "video/mp4")

    async def file_iterator():
        async for chunk in client.iter_download(message.media, chunk_size=2 * 1024 * 1024):
            yield chunk

    return StreamingResponse(
        file_iterator(),
        media_type=mime,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )

# ========================
# Simple M3U Playlist
# ========================
@app.get("/playlist.m3u", response_class=PlainTextResponse)
async def playlist(
    channel: str = "movies",
    limit: int = 20,
):
    """
    Extended M3U for a channel (for VLC/players).
    """
    channel_id = _resolve_channel_id(channel)
    items: List[Dict[str, Any]] = []
    async for message in client.iter_messages(channel_id, limit=limit):
        if _is_video_msg(message):
            items.append(_video_meta(message))

    lines = ["#EXTM3U"]
    for it in items:
        duration = int(it["duration"]) if it["duration"] else -1
        title = it["title"]
        url = f"/stream/{it['id']}?channel={channel}"
        lines.append(f"#EXTINF:{duration},{title}")
        lines.append(url)
    return "\n".join(lines)
