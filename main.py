import glob
import os
import uuid
from fastapi import FastAPI, HTTPException, BackgroundTasks, Query
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
import yt_dlp

try:
    # Lets yt-dlp impersonate a real browser's TLS fingerprint (requires the
    # curl_cffi package). Without this, requests from a datacenter IP (like
    # Render's) are far more likely to get flagged by TikTok's bot detection
    # and served an empty/challenge page instead of the real video data.
    from yt_dlp.networking.impersonate import ImpersonateTarget
    IMPERSONATE_TARGET = ImpersonateTarget.from_str('chrome')
except Exception:
    IMPERSONATE_TARGET = None

app = FastAPI()

# Allow connections from your React Native app
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Helper to automatically clean up files after sending them to the phone
def remove_file(path: str):
    if os.path.exists(path):
        os.remove(path)

# Maps the quality options the app's download sheet offers to a yt-dlp format
# selector. Falls back to the best available stream for anything unrecognized.
#
# Each per-height selector ends in an unconstrained `bestvideo+bestaudio/best`
# fallback. TikTok/Instagram short-form clips often only expose one or two
# renditions with no low-resolution options at all (e.g. a reel whose
# smallest available stream is 840p) — without this, requesting 720p or
# lower against such a video matched nothing at any step of the chain and
# failed outright with "Requested format is not available", even though a
# perfectly good (just higher-resolution) stream existed. Confirmed against
# live TikTok/Instagram URLs on 2026-10-14.
QUALITY_FORMATS = {
    '1080p': 'bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080][ext=mp4]/best[height<=1080]/bestvideo+bestaudio/best',
    '720p': 'bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best[height<=720]/bestvideo+bestaudio/best',
    '480p': 'bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/best[height<=480][ext=mp4]/best[height<=480]/bestvideo+bestaudio/best',
    '360p': 'bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/best[height<=360][ext=mp4]/best[height<=360]/bestvideo+bestaudio/best',
    '240p': 'bestvideo[height<=240][ext=mp4]+bestaudio[ext=m4a]/best[height<=240][ext=mp4]/best[height<=240]/bestvideo+bestaudio/best',
    '144p': 'bestvideo[height<=144][ext=mp4]+bestaudio[ext=m4a]/best[height<=144][ext=mp4]/best[height<=144]/bestvideo+bestaudio/best',
    'audio': 'bestaudio[ext=m4a]/bestaudio/best',
}
DEFAULT_FORMAT = 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best'


def _build_ydl_opts(output_template: str, quality: str | None):
    ydl_opts = {
        'format': QUALITY_FORMATS.get(quality, DEFAULT_FORMAT),
        'outtmpl': output_template,
        'merge_output_format': 'mp4',
        'quiet': True,
    }
    if IMPERSONATE_TARGET is not None:
        ydl_opts['impersonate'] = IMPERSONATE_TARGET
    return ydl_opts


# Lets the app show a thumbnail/title/quality picker before committing to a
# download. Read-only: never touches disk.
@app.get("/info")
async def extract_info(url: str = Query(...)):
    if not url:
        raise HTTPException(status_code=400, detail="URL is missing")

    ydl_opts = {'quiet': True}
    if IMPERSONATE_TARGET is not None:
        ydl_opts['impersonate'] = IMPERSONATE_TARGET

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Lookup failure: {type(e).__name__}: {e}")

    formats = [
        {'id': key, 'label': key, 'ext': 'm4a' if key == 'audio' else 'mp4'}
        for key in QUALITY_FORMATS
    ]

    return {
        'title': info.get('title') or url,
        'thumbnail': info.get('thumbnail'),
        'duration': info.get('duration'),
        'formats': formats,
    }


# GET with a query param: expo-file-system's background/resumable downloader
# on Android always issues a plain GET and ignores any configured httpMethod/body,
# so the video URL has to travel as a query parameter rather than a POST body.
@app.get("/download")
async def extract_and_download(
    background_tasks: BackgroundTasks,
    url: str = Query(...),
    quality: str = Query("best"),
):
    video_url = url
    if not video_url:
        raise HTTPException(status_code=400, detail="URL is missing")

    # Generate a completely unique local filename to prevent server user collision
    file_id = str(uuid.uuid4())
    output_template = f"downloads/{file_id}.%(ext)s"

    ydl_opts = _build_ydl_opts(output_template, quality)
    
    try:
        # Create storage folder if it doesn't exist
        os.makedirs("downloads", exist_ok=True)

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=True)
            # Find the true location of the freshly baked file. Don't guess the
            # extension from `info` (it can disagree with what actually landed on
            # disk depending on merge behavior) - glob by the unique file_id prefix
            # we gave outtmpl instead, which always matches whatever yt-dlp wrote.
            matches = glob.glob(f"downloads/{file_id}.*")
            filename = matches[0] if matches else None

        if not filename or not os.path.exists(filename):
            existing = os.listdir("downloads") if os.path.isdir("downloads") else []
            raise HTTPException(
                status_code=500,
                detail=(
                    f"File processing failed on server: expected output for id "
                    f"'{file_id}' (resolved ext '{info.get('ext') if info else '?'}') "
                    f"but found no matching file. downloads/ currently contains: {existing}"
                ),
            )

        # Queue the clean-up task so your server doesn't run out of storage space
        background_tasks.add_task(remove_file, filename)

        # Audio-only downloads land as .m4a, everything else as .mp4 - reflect
        # whatever extension actually landed on disk rather than hardcoding mp4.
        file_ext = os.path.splitext(filename)[1].lstrip('.') or 'mp4'
        media_type = 'audio/mp4' if file_ext == 'm4a' else 'video/mp4'

        # Send the file down the pipe directly as a physical binary download
        return FileResponse(
            path=filename,
            media_type=media_type,
            filename=f"downloaded_video_{file_id}.{file_ext}"
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Extraction failure: {type(e).__name__}: {e}")
