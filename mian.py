import os
import uuid
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
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

class DownloadRequest(BaseModel):
    url: str

# Helper to automatically clean up files after sending them to the phone
def remove_file(path: str):
    if os.path.exists(path):
        os.remove(path)

@app.post("/download")
async def extract_and_download(request: DownloadRequest, background_tasks: BackgroundTasks):
    video_url = request.url
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
            # Find the true location of the freshly baked .mp4 file
            filename = ydl.prepare_filename(info)
            # Handle potential edge cases where merging changes extension slightly
            filename = filename.rsplit('.', 1)[0] + '.mp4'

        if not os.path.exists(filename):
            raise HTTPException(status_code=500, detail="File processing failed on server")
            
        # Queue the clean-up task so your server doesn't run out of storage space
        background_tasks.add_task(remove_file, filename)
        
        # Send the file down the pipe directly as a physical binary download
        return FileResponse(
            path=filename, 
            media_type='video/mp4', 
            filename=f"downloaded_video_{file_id}.mp4"
        )
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Extraction failure: {str(e)}")
