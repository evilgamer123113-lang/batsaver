# BatSaver

Universal video saver - Pinterest, YouTube, TikTok, Instagram, X/Twitter, Reddit, Vimeo & more.

- `server.py` - Python server (serves the UI and /api/* endpoints)
- `downloads.py` - download engine (yt-dlp + ffmpeg)
- `requirements.txt` - Python deps
- `render.yaml` - free Render web-service config
- `index.html` - the app

## Run locally
pip install -r requirements.txt
python server.py
# open http://127.0.0.1:8787

## Deploy to Render (free)
1. New > Blueprint (or Web Service) from this repo.
2. Build: pip install -r requirements.txt   Start: python server.py
3. Env: HOST=0.0.0.0  (Render provides PORT)
4. Visit https://<your-name>.onrender.com