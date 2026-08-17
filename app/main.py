from __future__ import annotations

import base64
import binascii
import html
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
import zipfile
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import webvtt
import yt_dlp
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
WORK_ROOT = Path(tempfile.gettempdir()) / "clipnova_jobs"
WORK_ROOT.mkdir(parents=True, exist_ok=True)

COOKIE_ENV_NAME = "YTDLP_COOKIES_B64"
COOKIE_PATH = Path(tempfile.gettempdir()) / "clipnova_youtube_cookies.txt"

app = FastAPI(title="ClipNova", version="2.0.0")

JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
COOKIE_LOCK = threading.Lock()
JOB_TTL_SECONDS = 2 * 60 * 60

MAX_CLIPS = 6
MAX_CLIP_SECONDS = 10 * 60
MAX_TOTAL_SECONDS = 20 * 60


class AnalyzeRequest(BaseModel):
    url: str


class ClipRange(BaseModel):
    start: float = Field(ge=0)
    end: float = Field(gt=0)


class RenderRequest(BaseModel):
    url: str
    clips: list[ClipRange]
    quality: Literal["720", "1080", "1440", "2160", "best"] = "1080"
    captions: bool = True
    caption_language: str = "auto"
    caption_style: Literal["modern", "classic", "bold"] = "modern"
    confirm_rights: bool = False


def validate_youtube_url(url: str) -> str:
    url = url.strip()
    try:
        p = urlparse(url)
    except Exception:
        raise HTTPException(400, "Invalid URL.")
    host = (p.hostname or "").lower()
    allowed = {
        "youtube.com", "www.youtube.com", "m.youtube.com",
        "youtu.be", "music.youtube.com"
    }
    if p.scheme not in {"http", "https"} or host not in allowed:
        raise HTTPException(400, "Please enter a valid YouTube URL.")
    return url


def seconds_to_clock(value: float) -> str:
    value = max(0, int(value))
    h, rem = divmod(value, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def parse_vtt_time(value: str) -> float:
    parts = value.replace(",", ".").split(":")
    if len(parts) == 3:
        h, m, s = parts
    else:
        h = "0"
        m, s = parts
    return int(h) * 3600 + int(m) * 60 + float(s)


def srt_time(value: float) -> str:
    value = max(0.0, value)
    ms = int(round(value * 1000))
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def clean_caption_text(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def make_shifted_srt(vtt_path: Path, out_path: Path, start: float, end: float) -> int:
    rows = []
    seen = set()
    for cue in webvtt.read(str(vtt_path)):
        cue_start = parse_vtt_time(cue.start)
        cue_end = parse_vtt_time(cue.end)
        if cue_end <= start or cue_start >= end:
            continue
        text = clean_caption_text(cue.text)
        if not text:
            continue
        local_start = max(cue_start, start) - start
        local_end = min(cue_end, end) - start
        key = (round(local_start, 2), round(local_end, 2), text)
        if key in seen:
            continue
        seen.add(key)
        rows.append((local_start, local_end, text))

    with out_path.open("w", encoding="utf-8") as f:
        for idx, (a, b, text) in enumerate(rows, 1):
            f.write(f"{idx}\n{srt_time(a)} --> {srt_time(b)}\n{text}\n\n")
    return len(rows)


def youtube_auth_configured() -> bool:
    return bool(os.getenv(COOKIE_ENV_NAME, "").strip())


def materialize_cookie_file() -> Path | None:
    encoded = os.getenv(COOKIE_ENV_NAME, "").strip()
    if not encoded:
        return None
    encoded = re.sub(r"\\s+", "", encoded)

    with COOKIE_LOCK:
        if COOKIE_PATH.exists() and COOKIE_PATH.stat().st_size > 20:
            return COOKIE_PATH

        try:
            raw = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise RuntimeError(
                f"{COOKIE_ENV_NAME} is not valid Base64. Re-create the Railway secret from a Netscape cookies.txt file."
            ) from exc

        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise RuntimeError(
                f"{COOKIE_ENV_NAME} did not decode to a UTF-8 cookies.txt file."
            ) from exc

        normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip() + "\n"
        first_line = normalized.splitlines()[0].strip() if normalized.strip() else ""
        if first_line not in {"# Netscape HTTP Cookie File", "# HTTP Cookie File"}:
            raise RuntimeError(
                "YouTube cookies are not in Netscape/Mozilla cookies.txt format."
            )
        if "youtube.com" not in normalized.lower():
            raise RuntimeError(
                "The configured cookies file does not appear to contain YouTube cookies."
            )

        COOKIE_PATH.write_text(normalized, encoding="utf-8", newline="\n")
        try:
            os.chmod(COOKIE_PATH, 0o600)
        except OSError:
            pass
        return COOKIE_PATH


def ytdlp_cookie_cli_args() -> list[str]:
    cookie_file = materialize_cookie_file()
    return ["--cookies", str(cookie_file)] if cookie_file else []


def ytdlp_python_options() -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
    }
    cookie_file = materialize_cookie_file()
    if cookie_file:
        opts["cookiefile"] = str(cookie_file)
    return opts


def set_job(job_id: str, **updates):
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(updates)


def cleanup_old_jobs():
    cutoff = time.time() - JOB_TTL_SECONDS
    stale = []
    with JOBS_LOCK:
        for jid, job in JOBS.items():
            if job.get("created_at", 0) < cutoff:
                stale.append((jid, job.get("workdir")))
        for jid, _ in stale:
            JOBS.pop(jid, None)
    for _, workdir in stale:
        if workdir:
            shutil.rmtree(workdir, ignore_errors=True)


def classify_processing_error(raw: str) -> tuple[str, str]:
    text = (raw or "").strip()
    lower = text.lower()

    if "sign in to confirm you're not a bot" in lower:
        if youtube_auth_configured():
            return (
                "YOUTUBE_AUTH_REJECTED",
                "YouTube rejected the server session. The admin YouTube cookies may have expired or rotated. Refresh the Railway YouTube cookie secret and try again.",
            )
        return (
            "YOUTUBE_AUTH_REQUIRED",
            "YouTube blocked this cloud server with a bot check. The ClipNova admin must configure the private YouTube authentication secret before rendering.",
        )

    if "cookies are no longer valid" in lower or "cookies have likely been rotated" in lower:
        return (
            "YOUTUBE_COOKIES_EXPIRED",
            "The private YouTube session used by ClipNova has expired or rotated. The admin needs to refresh the Railway YouTube cookie secret.",
        )

    if "http error 403" in lower or "forbidden" in lower:
        return (
            "YOUTUBE_403",
            "YouTube refused the media stream from this server. Try again later; if it continues, the admin should refresh the private YouTube authentication session.",
        )

    if "requested format is not available" in lower:
        return (
            "FORMAT_UNAVAILABLE",
            "The selected quality is not available for this video. Try 1080p, 720p, or Best available.",
        )

    if "private video" in lower or "members-only" in lower or "members only" in lower:
        return (
            "VIDEO_RESTRICTED",
            "This video is restricted and cannot be processed with the current server session.",
        )

    if "video unavailable" in lower:
        return (
            "VIDEO_UNAVAILABLE",
            "This YouTube video is unavailable to the server.",
        )

    return (
        "PROCESSING_FAILED",
        "The clip could not be processed. Please try a shorter clip or another quality. The server log contains the technical error for the admin.",
    )


def run_cmd(cmd: list[str], cwd: Path | None = None):
    proc = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        tail = proc.stdout[-5000:] if proc.stdout else "Unknown command error"
        print("[ClipNova V2] subprocess failed:", flush=True)
        print(tail, flush=True)
        raise RuntimeError(tail)
    return proc.stdout


def quality_format(q: str) -> str:
    if q == "best":
        return "bestvideo+bestaudio/best"
    h = int(q)
    return f"bestvideo[height<={h}]+bestaudio/best[height<={h}]"


def subtitle_lang_candidates(info: dict, requested: str) -> list[str]:
    if requested and requested != "auto":
        return [requested, f"{requested}.*"]
    lang = (info.get("language") or "").strip()
    candidates = []
    if lang:
        candidates += [lang, f"{lang}.*"]
    candidates += ["en", "en.*", "ur", "ur.*", "hi", "hi.*"]
    dedup = []
    for x in candidates:
        if x and x not in dedup:
            dedup.append(x)
    return dedup


def fetch_subtitle(url: str, info: dict, requested: str, workdir: Path) -> Path | None:
    subdir = workdir / "subs"
    subdir.mkdir(exist_ok=True)
    cookie_args = ytdlp_cookie_cli_args()

    for lang in subtitle_lang_candidates(info, requested):
        for old in subdir.glob("*"):
            old.unlink(missing_ok=True)

        cmd = [
            "yt-dlp",
            "--no-playlist",
            "--skip-download",
            "--retries", "3",
            "--socket-timeout", "30",
            *cookie_args,
            "--write-subs",
            "--write-auto-subs",
            "--sub-langs", lang,
            "--sub-format", "vtt",
            "-o", str(subdir / "subtitle"),
            url,
        ]
        try:
            run_cmd(cmd)
        except Exception:
            continue

        candidates = list(subdir.glob("*.vtt"))
        if candidates:
            return candidates[0]

    return None


def ffmpeg_caption_style(style: str) -> str:
    if style == "classic":
        return "FontName=DejaVu Sans,FontSize=20,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BorderStyle=1,Outline=2,Shadow=0,Alignment=2,MarginV=40"
    if style == "bold":
        return "FontName=DejaVu Sans,FontSize=26,Bold=1,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BorderStyle=1,Outline=4,Shadow=0,Alignment=2,MarginV=48"
    return "FontName=DejaVu Sans,FontSize=24,Bold=1,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BackColour=&H80000000,BorderStyle=3,Outline=1,Shadow=0,Alignment=2,MarginV=46"


def burn_captions(src: Path, srt: Path, dst: Path, style: str):
    escaped = str(srt).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")
    vf = f"subtitles='{escaped}':force_style='{ffmpeg_caption_style(style)}'"
    cmd = [
        "ffmpeg", "-y", "-i", str(src),
        "-vf", vf,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(dst),
    ]
    run_cmd(cmd)


def normalize_mp4(src: Path, dst: Path):
    cmd = [
        "ffmpeg", "-y", "-i", str(src),
        "-c", "copy", "-movflags", "+faststart",
        str(dst),
    ]
    try:
        run_cmd(cmd)
    except Exception:
        shutil.copy2(src, dst)


def worker(job_id: str, req: RenderRequest):
    workdir = Path(JOBS[job_id]["workdir"])
    try:
        set_job(job_id, state="running", progress=3, message="Reading video information…")

        with yt_dlp.YoutubeDL(ytdlp_python_options()) as ydl:
            info = ydl.extract_info(req.url, download=False)

        title = info.get("title") or "YouTube clip"
        set_job(job_id, progress=7, message=f"Preparing {len(req.clips)} clip(s)…", title=title)

        subtitle_file = None
        if req.captions:
            set_job(job_id, progress=10, message="Looking for captions…")
            subtitle_file = fetch_subtitle(req.url, info, req.caption_language, workdir)

        outputs = []
        warnings = []
        if req.captions and not subtitle_file:
            warnings.append(
                "No suitable subtitle/auto-caption track was available; clips will be exported without burned captions."
            )

        base_progress = 12
        span = 80 / max(1, len(req.clips))
        cookie_args = ytdlp_cookie_cli_args()

        for idx, clip in enumerate(req.clips, 1):
            clip_dir = workdir / f"clip_{idx:02d}"
            clip_dir.mkdir(exist_ok=True)

            set_job(
                job_id,
                progress=int(base_progress + (idx - 1) * span),
                message=f"Downloading selected section {idx}/{len(req.clips)}…",
            )

            out_template = str(clip_dir / "source.%(ext)s")
            cmd = [
                "yt-dlp",
                "--no-playlist",
                "--retries", "3",
                "--fragment-retries", "3",
                "--retry-sleep", "1",
                "--socket-timeout", "30",
                *cookie_args,
                "-f", quality_format(req.quality),
                "--download-sections", f"*{seconds_to_clock(clip.start)}-{seconds_to_clock(clip.end)}",
                "--force-keyframes-at-cuts",
                "--merge-output-format", "mp4",
                "-o", out_template,
                req.url,
            ]
            run_cmd(cmd)

            candidates = [
                p for p in clip_dir.glob("source.*")
                if p.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov"}
            ]
            if not candidates:
                raise RuntimeError(f"Clip {idx}: media file was not created.")

            src = candidates[0]
            final = clip_dir / f"ClipNova_{idx:02d}.mp4"

            if req.captions and subtitle_file:
                srt = clip_dir / "captions.srt"
                caption_count = make_shifted_srt(subtitle_file, srt, clip.start, clip.end)

                if caption_count:
                    set_job(
                        job_id,
                        progress=int(base_progress + (idx - 0.45) * span),
                        message=f"Burning captions into clip {idx}/{len(req.clips)}…",
                    )
                    burn_captions(src, srt, final, req.caption_style)
                else:
                    normalize_mp4(src, final)
                    warnings.append(
                        f"Clip {idx}: no caption lines existed inside this selected time range."
                    )
            else:
                normalize_mp4(src, final)

            outputs.append(final)

            set_job(
                job_id,
                progress=int(base_progress + idx * span),
                message=f"Finished clip {idx}/{len(req.clips)}.",
            )

        if len(outputs) == 1:
            result = outputs[0]
            download_name = f"ClipNova_{job_id[:8]}.mp4"
        else:
            result = workdir / f"ClipNova_{job_id[:8]}.zip"
            with zipfile.ZipFile(result, "w", compression=zipfile.ZIP_STORED) as zf:
                for p in outputs:
                    zf.write(p, arcname=p.name)
            download_name = result.name

        set_job(
            job_id,
            state="done",
            progress=100,
            message="Your clips are ready.",
            result=str(result),
            download_name=download_name,
            warnings=warnings,
        )

    except Exception as exc:
        raw_error = str(exc)[-5000:]
        code, friendly = classify_processing_error(raw_error)
        print(f"[ClipNova V2] job {job_id} failed with {code}", flush=True)
        print(raw_error, flush=True)

        set_job(
            job_id,
            state="error",
            progress=100,
            message="Render stopped",
            error_code=code,
            error=friendly,
        )


@app.get("/api/health")
def health():
    return {"ok": True, "service": "ClipNova", "version": "2.0.0"}


@app.get("/api/server-status")
def server_status():
    cookie_status = "not_configured"
    if youtube_auth_configured():
        try:
            materialize_cookie_file()
            cookie_status = "configured"
        except Exception:
            cookie_status = "invalid"

    return {
        "ok": True,
        "version": "2.0.0",
        "youtube_auth": cookie_status,
        "ffmpeg": shutil.which("ffmpeg") is not None,
        "ytdlp": shutil.which("yt-dlp") is not None,
    }


@app.post("/api/analyze")
def analyze(req: AnalyzeRequest):
    url = validate_youtube_url(req.url)

    try:
        with yt_dlp.YoutubeDL(ytdlp_python_options()) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:
        code, friendly = classify_processing_error(str(exc))
        raise HTTPException(400, {"code": code, "message": friendly})

    duration = int(info.get("duration") or 0)
    if not duration:
        raise HTTPException(400, "Could not determine video duration.")

    return {
        "id": info.get("id"),
        "title": info.get("title"),
        "thumbnail": info.get("thumbnail"),
        "duration": duration,
        "duration_text": seconds_to_clock(duration),
        "channel": info.get("channel") or info.get("uploader"),
        "language": info.get("language"),
        "webpage_url": info.get("webpage_url") or url,
    }


@app.post("/api/render")
def render(req: RenderRequest):
    cleanup_old_jobs()
    req.url = validate_youtube_url(req.url)

    if not req.confirm_rights:
        raise HTTPException(
            400,
            "Please confirm that you own or have permission to process this video.",
        )

    if not req.clips or len(req.clips) > MAX_CLIPS:
        raise HTTPException(400, f"Choose between 1 and {MAX_CLIPS} clips.")

    total = 0.0
    for i, c in enumerate(req.clips, 1):
        if c.end <= c.start:
            raise HTTPException(400, f"Clip {i}: end time must be after start time.")

        length = c.end - c.start
        if length > MAX_CLIP_SECONDS:
            raise HTTPException(400, f"Clip {i} is longer than 10 minutes.")
        total += length

    if total > MAX_TOTAL_SECONDS:
        raise HTTPException(400, "Total selected output is longer than 20 minutes.")

    try:
        materialize_cookie_file()
    except Exception as exc:
        raise HTTPException(
            503,
            {
                "code": "YOUTUBE_AUTH_CONFIG_INVALID",
                "message": str(exc),
            },
        )

    job_id = uuid.uuid4().hex
    workdir = WORK_ROOT / job_id
    workdir.mkdir(parents=True, exist_ok=True)

    with JOBS_LOCK:
        JOBS[job_id] = {
            "id": job_id,
            "created_at": time.time(),
            "state": "queued",
            "progress": 0,
            "message": "Queued…",
            "workdir": str(workdir),
            "warnings": [],
        }

    threading.Thread(target=worker, args=(job_id, req), daemon=True).start()
    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(404, "Job not found or expired.")

        public = {
            k: v for k, v in job.items()
            if k not in {"workdir", "result"}
        }

        if job.get("state") == "done":
            public["download_url"] = f"/api/jobs/{job_id}/download"

        return public


@app.get("/api/jobs/{job_id}/download")
def download(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job or job.get("state") != "done":
            raise HTTPException(404, "Finished file not found.")

        result = Path(job["result"])
        name = job.get("download_name") or result.name

    if not result.exists():
        raise HTTPException(404, "Finished file expired.")

    media_type = "application/zip" if result.suffix.lower() == ".zip" else "video/mp4"
    return FileResponse(result, media_type=media_type, filename=name)


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
