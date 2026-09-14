#!/usr/bin/env python3
"""downloads.py — BatSaver download engine (logic only, no UI).

Powers /api/dl on the local server. Uses yt-dlp (+ ffmpeg) to download from
YouTube, TikTok, Instagram, X, Reddit, Pinterest, Vimeo, and anything else
yt-dlp supports.
"""
import importlib.util
import os
import shutil
import subprocess
import sys

CHUNK = 65536


def have_ytdlp():
    try:
        return importlib.util.find_spec("yt_dlp") is not None
    except Exception:
        return False


def find_ffmpeg():
    ff = shutil.which("ffmpeg")
    if ff:
        return ff
    try:
        import imageio_ffmpeg

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.exists(exe):
            return exe
    except Exception:
        pass
    return None


class DlError(Exception):
    pass


def dl_stream(url, chunk_size=CHUNK, ffmpeg=None):
    """Yield the downloaded media bytes for ``url`` as an iterator."""
    if not have_ytdlp():
        raise DlError("yt-dlp is not installed (pip install yt-dlp)")
    ffmpeg = ffmpeg or find_ffmpeg()
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "-o", "-",
        "--no-playlist", "--no-progress", "--no-warnings", "--quiet",
    ]
    if ffmpeg:
        cmd += ["--ffmpeg-location", os.path.dirname(ffmpeg)]
    cmd += [url]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except Exception as e:
        raise DlError("could not start yt-dlp: %s" % e)
    try:
        while True:
            chunk = proc.stdout.read(chunk_size)
            if not chunk:
                break
            yield chunk
    finally:
        proc.kill()
        proc.wait()


def media_info(url, ffmpeg=None):
    """Quick yt-dlp metadata -> {platform,title,author,media:...} or None."""
    if not have_ytdlp():
        return None
    ffmpeg = ffmpeg or find_ffmpeg()
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "-J", "--no-playlist", "--no-warnings", "--socket-timeout", "25", url,
    ]
    if ffmpeg:
        cmd += ["--ffmpeg-location", os.path.dirname(ffmpeg)]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=75)
        if out.returncode != 0:
            return None
        import json

        data = json.loads(out.stdout)
    except Exception:
        return None

    formats = [
        f for f in (data.get("formats") or [])
        if f.get("url") and (f.get("protocol") or "").startswith("http")
        and (f.get("ext") in ("mp4", "webm"))
    ]
    prog = [f for f in formats if f.get("vcodec") != "none" and f.get("acodec") != "none"]
    chosen = None
    if prog:
        prog.sort(key=lambda f: (f.get("height") or 0, f.get("tbr") or 0), reverse=True)
        chosen = prog[0]
    else:
        vids = [f for f in formats if f.get("vcodec") != "none"]
        if vids:
            vids.sort(key=lambda f: (f.get("height") or 0, f.get("tbr") or 0), reverse=True)
            chosen = vids[0]
    if not chosen or not chosen.get("url"):
        return None

    return {
        "platform": None,  # caller sets the detected platform
        "title": data.get("title"),
        "author": data.get("uploader") or data.get("uploader_id"),
        "media": [{
            "url": chosen["url"],
            "type": "video",
            "quality": (str(chosen.get("height") or 0) + "p") if chosen.get("height") else "source",
        }],
    }