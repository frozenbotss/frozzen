from flask import Flask, request, Response, jsonify, send_file
import asyncio
import aiohttp
import os
import time
import threading
import subprocess
from asyncio.subprocess import DEVNULL
import hashlib

app = Flask(__name__)

# —————— Configuration ——————
BOT_TOKEN        = os.environ.get("BOT_TOKEN", "8225942232:AAG2aIGNlNRecZ-J8WIFz2gc3-x65s6RCGM")
CHAT_ID          = os.environ.get("CHAT_ID",   "7634862283")
TELEGRAM_API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
FILE_BASE_URL    = f"https://api.telegram.org/file/bot{BOT_TOKEN}"
BASE_DIR         = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR     = os.path.join(BASE_DIR, "downloads")
CACHE_DIR        = os.path.join(BASE_DIR, "cache")

os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(CACHE_DIR, exist_ok=True)


def url_to_hash(url: str) -> str:
    """Generate a unique hash for a URL."""
    return hashlib.md5(url.encode('utf-8')).hexdigest()

async def send_doown_command(url: str):
    async with aiohttp.ClientSession() as session:
        await session.post(
            f"{TELEGRAM_API_URL}/sendMessage",
            json={"chat_id": CHAT_ID, "text": f"/doown {url}"}
        )


async def send_down_command(url: str):
    async with aiohttp.ClientSession() as session:
        await session.post(
            f"{TELEGRAM_API_URL}/sendMessage",
            json={"chat_id": CHAT_ID, "text": f"/down {url}"}
        )

async def flush_updates(session):
    resp = await session.get(f"{TELEGRAM_API_URL}/getUpdates")
    data = await resp.json()
    if data.get("result"):
        last_id = data["result"][-1]["update_id"]
        await session.get(
            f"{TELEGRAM_API_URL}/getUpdates",
            params={"offset": last_id + 1}
        )

async def wait_for_audio_file(timeout: int = 10) -> str | None:
    async with aiohttp.ClientSession() as session:
        await flush_updates(session)
        start = time.time()
        offset = None
        while time.time() - start < timeout:
            params = {"offset": offset} if offset else {}
            resp = await session.get(f"{TELEGRAM_API_URL}/getUpdates", params=params)
            data    = await resp.json()
            updates = data.get("result", [])
            for upd in updates:
                offset = upd["update_id"] + 1
                msg = upd.get("message", {})
                if "audio" in msg or "voice" in msg:
                    return (msg.get("audio") or msg.get("voice"))["file_id"]
            await asyncio.sleep(2)
    return None

async def get_file_url(file_id: str) -> str | None:
    async with aiohttp.ClientSession() as session:
        resp = await session.get(
            f"{TELEGRAM_API_URL}/getFile",
            params={"file_id": file_id}
        )
        data = await resp.json()
        if not data.get("ok") or "result" not in data:
            print(f"[ERROR] get_file_url failed: {data}")
            return None
        path = data["result"].get("file_path")
        return f"{FILE_BASE_URL}/{path}" if path else None

async def download_file_stream(url: str, dest_path: str) -> bool:
    async with aiohttp.ClientSession() as session:
        async with session.get(url, allow_redirects=True) as resp:
            if resp.status != 200:
                print(f"[ERROR] HTTP {resp.status} for {url}")
                return False
            with open(dest_path, "wb") as f:
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    f.write(chunk)
            return True

@app.route("/download")
def down():
    yt_url = request.args.get("url")
    if not yt_url:
        return jsonify({"error": "Missing YouTube URL"}), 400

    # Compute cache path based on URL hash
    video_hash = url_to_hash(yt_url)
    cached_mp3 = os.path.join(CACHE_DIR, f"{video_hash}.mp3")

    # If cached file exists, return it immediately
    if os.path.exists(cached_mp3):
        return send_file(cached_mp3, mimetype="audio/mpeg", as_attachment=True)

    async def process():
        # Trigger Telegram bot to produce .m4a
        await send_down_command(yt_url)
        # Wait for audio file_id
        file_id = await wait_for_audio_file()
        if not file_id:
            return jsonify({"error": "Timeout waiting for audio"}), 504
        # Download .m4a
        download_url = await get_file_url(file_id)
        if not download_url:
            return jsonify({"error": "Failed to get download URL"}), 500
        m4a_path = os.path.join(DOWNLOAD_DIR, f"{file_id}.m4a")
        if not await download_file_stream(download_url, m4a_path):
            return jsonify({"error": "Failed to download .m4a"}), 500
        # Convert to MP3, store directly to cache
        ffmpeg_cmd = [
            "ffmpeg", "-nostdin",
            "-probesize", "32k", "-analyzeduration", "0",
            "-i", m4a_path,
            "-vn", "-codec:a", "libmp3lame", "-b:a", "56k",
            "-bufsize", "64k", "-rtbufsize", "64k",
            "-threads", "1",
            cached_mp3
        ]
        subprocess.run(ffmpeg_cmd, stdout=DEVNULL, stderr=DEVNULL, check=True)
        # Cleanup .m4a
        try:
            os.remove(m4a_path)
        except OSError:
            pass
        # Return cached MP3
        return send_file(cached_mp3, mimetype="audio/mpeg", as_attachment=True)

    return asyncio.run(process())

@app.route("/raw-audio")
def raw_audio():
    spotify_url = request.args.get("url")
    if not spotify_url:
        return jsonify({"error": "Missing Spotify URL"}), 400

    # Hash the Spotify URL to create a unique cache key
    audio_hash = url_to_hash(spotify_url)
    cached_file_path = os.path.join(CACHE_DIR, f"{audio_hash}.mp3")

    # Return cached file if exists
    if os.path.exists(cached_file_path):
        return send_file(cached_file_path, mimetype="audio/mpeg", as_attachment=True)

    async def process():
        await send_doown_command(spotify_url)

        file_id = await wait_for_audio_file()
        if not file_id:
            return jsonify({"error": "Timeout waiting for audio"}), 504

        download_url = await get_file_url(file_id)
        if not download_url:
            return jsonify({"error": "Failed to get download URL"}), 500

        # Attempt to use the Telegram file extension if available
        raw_path = cached_file_path  # default path
        if download_url.endswith(".ogg"):
            raw_path = cached_file_path.replace(".raw", ".ogg")
        elif download_url.endswith(".m4a"):
            raw_path = cached_file_path.replace(".raw", ".m4a")

        if not await download_file_stream(download_url, raw_path):
            return jsonify({"error": "Failed to download raw audio"}), 500

        return send_file(raw_path, mimetype="audio/mpeg", as_attachment=True)

    return asyncio.run(process())


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))








