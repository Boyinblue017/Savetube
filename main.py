"""
SaveTube Backend — FastAPI + yt-dlp
Run:  uvicorn main:app --reload --port 8000
"""

import os
import uuid
import threading
from pathlib import Path
from typing import Optional

import yt_dlp
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ── Config ──────────────────────────────────────────────────────────────────
DOWNLOADS_DIR = Path("downloads")
DOWNLOADS_DIR.mkdir(exist_ok=True)

# ── Cookie settings ──────────────────────────────────────────────────────────
# Option A (easiest): pull cookies live from your browser
# Set COOKIE_BROWSER to one of: "chrome", "firefox", "edge", "brave", "opera"
COOKIE_BROWSER = "chrome"   # ← change to whichever browser you use

# Option B: point to an exported cookies.txt file (Netscape format)
# Leave as None to use Option A instead
COOKIE_FILE = Path(__file__).parent / "cookies.txt"   # e.g. Path("cookies.txt")

def _cookie_opts() -> dict:
    """Return the right cookie option for yt-dlp."""
    if COOKIE_FILE and Path(COOKIE_FILE).exists():
        return {"cookiefile": str(COOKIE_FILE)}
    return {"cookiesfrombrowser": (COOKIE_BROWSER,)}

# In-memory job store  { job_id: { status, progress, filename, error } }
jobs: dict = {}

app = FastAPI(title="SaveTube API", version="1.0.0")

# Allow the HTML frontend (opened from file:// or localhost) to call the API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve downloaded files
app.mount("/files", StaticFiles(directory=DOWNLOADS_DIR), name="files")


# ── Helpers ──────────────────────────────────────────────────────────────────

def _format_seconds(secs: Optional[float]) -> str:
    if not secs:
        return "0:00"
    secs = int(secs)
    h, m, s = secs // 3600, (secs % 3600) // 60, secs % 60
    if h:
        return f"{h}:{m:02}:{s:02}"
    return f"{m}:{s:02}"


def _human_size(b: Optional[int]) -> str:
    if not b:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} TB"


def _quality_label(fmt: dict) -> str:
    height = fmt.get("height")
    acodec = fmt.get("acodec", "none")
    vcodec = fmt.get("vcodec", "none")

    if vcodec == "none" and acodec != "none":
        abr = fmt.get("abr") or fmt.get("tbr") or 0
        return f"Audio Only — {int(abr)}kbps"
    if height:
        fps = fmt.get("fps") or 0
        fps_str = f" {int(fps)}fps" if fps and fps > 30 else ""
        return f"{height}p{fps_str}"
    return fmt.get("format_note") or fmt.get("format_id", "Unknown")


# ── Routes ───────────────────────────────────────────────────────────────────

@app.get("/")
def root():
    return {"message": "SaveTube API is running ✔"}


@app.get("/info")
def get_info(url: str):
    """
    Fetch video metadata + available quality options.
    Returns everything the frontend needs to render the video card.
    """
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        **_cookie_opts(),
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Build quality list — deduplicate by height, keep best filesize estimate
    seen_heights = {}
    audio_fmt = None

    for fmt in info.get("formats", []):
        h = fmt.get("height")
        vcodec = fmt.get("vcodec", "none")
        acodec = fmt.get("acodec", "none")
        size = fmt.get("filesize") or fmt.get("filesize_approx") or 0

        # Best audio-only
        if vcodec == "none" and acodec != "none":
            if not audio_fmt or size > (audio_fmt.get("filesize") or 0):
                audio_fmt = fmt
            continue

        if h and h >= 360:
            if h not in seen_heights or size > (seen_heights[h].get("filesize") or 0):
                seen_heights[h] = fmt

    # Sort heights descending
    sorted_fmts = [seen_heights[h] for h in sorted(seen_heights.keys(), reverse=True)]
    if audio_fmt:
        sorted_fmts.append(audio_fmt)

    qualities = []
    for fmt in sorted_fmts:
        h = fmt.get("height")
        abr = fmt.get("abr") or fmt.get("tbr") or 0
        fps = fmt.get("fps") or 0
        size = fmt.get("filesize") or fmt.get("filesize_approx") or 0
        vcodec = fmt.get("vcodec", "none")
        acodec = fmt.get("acodec", "none")

        is_audio = vcodec == "none"

        label = _quality_label(fmt)
        badge = None
        if not is_audio:
            if h and h >= 2160:
                badge = "4K"
            elif h and h >= 1080:
                badge = "HD"
        else:
            badge = "♪"

        qualities.append({
            "format_id": fmt["format_id"],
            "label": label,
            "res": f"MP3 {int(abr)}kbps" if is_audio else f"{h}p",
            "fps": f"{int(fps)}fps" if fps and fps > 1 and not is_audio else None,
            "size": _human_size(size) if size else "~",
            "badge": badge,
            "is_audio": is_audio,
        })

    thumbnail = info.get("thumbnail") or (info.get("thumbnails") or [{}])[-1].get("url")

    return {
        "title": info.get("title", "Unknown"),
        "channel": info.get("uploader") or info.get("channel", ""),
        "duration": _format_seconds(info.get("duration")),
        "views": f"{info.get('view_count', 0):,} views" if info.get("view_count") else "",
        "thumbnail": thumbnail,
        "qualities": qualities,
    }


class DownloadRequest(BaseModel):
    url: str
    format_id: str
    is_audio: bool = False


def _run_download(job_id: str, url: str, format_id: str, is_audio: bool):
    """Background thread: download and update job progress."""

    def progress_hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes", 0)
            pct = (downloaded / total * 100) if total else 0
            speed = d.get("_speed_str", "")
            eta = d.get("_eta_str", "")
            jobs[job_id].update({
                "status": "downloading",
                "progress": round(pct, 1),
                "speed": speed,
                "eta": eta,
            })
        elif d["status"] == "finished":
            jobs[job_id]["status"] = "processing"

    out_tmpl = str(DOWNLOADS_DIR / "%(title).80s [%(id)s].%(ext)s")

    if is_audio:
        ydl_opts = {
            "format": format_id,
            "outtmpl": out_tmpl,
            "quiet": True,
            "no_warnings": True,
            "progress_hooks": [progress_hook],
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "320",
            }],
            **_cookie_opts(),
        }
    else:
        # Merge video + best audio for the selected height
        ydl_opts = {
            "format": f"{format_id}+bestaudio/best[height<={format_id}]/{format_id}",
            "outtmpl": out_tmpl,
            "quiet": True,
            "no_warnings": True,
            "progress_hooks": [progress_hook],
            "merge_output_format": "mp4",
            **_cookie_opts(),
        }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            # Normalise extension after possible merge
            stem = Path(filename).stem
            for ext in ("mp4", "mkv", "webm", "mp3", "m4a"):
                candidate = DOWNLOADS_DIR / f"{stem}.{ext}"
                if candidate.exists():
                    filename = str(candidate)
                    break

        jobs[job_id].update({
            "status": "done",
            "progress": 100,
            "filename": Path(filename).name,
        })
    except Exception as e:
        jobs[job_id].update({"status": "error", "error": str(e)})


@app.post("/download")
def start_download(req: DownloadRequest, background_tasks: BackgroundTasks):
    """
    Kick off a download job.
    Returns a job_id to poll for progress.
    """
    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status": "queued", "progress": 0}

    thread = threading.Thread(
        target=_run_download,
        args=(job_id, req.url, req.format_id, req.is_audio),
        daemon=True,
    )
    thread.start()

    return {"job_id": job_id}


@app.get("/progress/{job_id}")
def get_progress(job_id: str):
    """Poll download progress for a given job."""
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.get("/videos")
def list_videos():
    """List all videos saved to the downloads folder."""
    videos = []
    for f in sorted(DOWNLOADS_DIR.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if f.suffix.lower() in (".mp4", ".mkv", ".webm", ".mp3", ".m4a"):
            stat = f.stat()
            videos.append({
                "filename": f.name,
                "size": _human_size(stat.st_size),
                "url": f"/files/{f.name}",
            })
    return {"videos": videos}


@app.delete("/videos/{filename}")
def delete_video(filename: str):
    """Delete a downloaded video from disk."""
    # Sanitise — no path traversal
    safe = Path(filename).name
    target = DOWNLOADS_DIR / safe
    if not target.exists():
        raise HTTPException(status_code=404, detail="File not found")
    target.unlink()
    return {"deleted": safe}