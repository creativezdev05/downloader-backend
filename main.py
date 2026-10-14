import glob
import os
import uuid
from fastapi import FastAPI, HTTPException, BackgroundTasks, Query
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
import yt_dlp

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

# GET with a query param: expo-file-system's background/resumable downloader
# on Android always issues a plain GET and ignores any configured httpMethod/body,
# so the video URL has to travel as a query parameter rather than a POST body.
@app.get("/download")
async def extract_and_download(background_tasks: BackgroundTasks, url: str = Query(...)):
    video_url = url
    if not video_url:
        raise HTTPException(status_code=400, detail="URL is missing")
        
    # Generate a completely unique local filename to prevent server user collision
    file_id = str(uuid.uuid4())
    output_template = f"downloads/{file_id}.%(ext)s"
    
    # Configure yt-dlp to grab high-quality streams and merge them automatically using FFmpeg
    ydl_opts = {
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'outtmpl': output_template,
        'merge_output_format': 'mp4',
        'quiet': True
    }
    
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

        # Send the file down the pipe directly as a physical binary download
        return FileResponse(
            path=filename,
            media_type='video/mp4',
            filename=f"downloaded_video_{file_id}.mp4"
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Extraction failure: {type(e).__name__}: {e}")
