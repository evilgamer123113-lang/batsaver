#!/usr/bin/env python3
"""PinSaver local server. Zero dependencies -- Python 3 standard library only.

Endpoints (also mirrored under /api/ for Netlify compat):
  GET /                -> index.html
  GET /ping            -> "pong"
  GET /proxy?url=      -> fetches <url> server-side, ACAO: *, Range support
  GET /resolve?url=    -> platform detection + media extraction (JSON)
Run:  python server.py   (or double-click Start PinSaver.bat)
"""

import http.server
import json
import os
import re
import socketserver
import urllib.parse
import urllib.request

import downloads

HOST = "127.0.0.1"
PORT = 8787
ROOT = os.path.dirname(os.path.abspath(__file__))
FILE = os.path.join(ROOT, "index.html")
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140 Safari/537.36"
)
CHUNK = 65536


def _outbound_headers(range_header=None, referer="https://www.pinterest.com/"):
    headers = {
        "User-Agent": UA,
        "Accept-Encoding": "identity",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": referer,
    }
    if range_header:
        headers["Range"] = range_header
    return headers


# ---------- platform resolvers ----------

def _pick(pattern, html):
    m = re.search(pattern, html, re.I | re.S)
    return m.group(1) if m else None


def _og(html, prop):
    m = re.search(r'<meta[^>]+property="%s"[^>]+content="([^"]+)"' % re.escape(prop), html, re.I)
    if not m:
        m = re.search(r'<meta[^>]+content="([^"]+)"[^>]+property="%s"' % re.escape(prop), html, re.I)
    return m.group(1) if m else None


def _title(html):
    return _og(html, "og:title") or _pick(r"<title>([^<]+)</title>", html)


def _author(html):
    return _og(html, "article:author") or _og(html, "og:site_name")


def _media(vid=None, img=None):
    media = []
    if vid:
        media.append({"url": vid, "type": "video"})
    if img:
        media.append({"url": img, "type": "image"})
    return media


def parse_tiktok(html, url):
    vid = _og(html, "og:video") or _og(html, "og:video:url") or _og(html, "og:video:secure_url")
    img = _og(html, "og:image")
    media = _media(vid, img if not vid else None)
    if not media:
        m = re.search(r'__UNIVERSAL_DATA_FOR_REHYDRATION__">\s*<script[^>]*>(.*?)</script>', html, re.S)
        if m:
            try:
                d = json.loads(m.group(1))
                item = (d or {}).get("__DEFAULT_SCOPE__", {}).get("webapp.video-detail", {}).get("itemInfo", {}).get("itemStruct")
                if item:
                    play = (item.get("video") or {}).get("playAddr")
                    if play:
                        media.append({"url": play, "type": "video"})
                    dl = (item.get("video") or {}).get("downloadAddr")
                    if dl:
                        media.append({"url": dl, "type": "video", "quality": "download"})
            except Exception:
                pass
    return {"platform": "tiktok", "title": _title(html), "author": _author(html), "media": media}


def _balanced_json(html, marker):
    """Find '{...}' starting after marker, balanced for nested braces."""
    i = html.find(marker)
    if i < 0:
        return None
    start = html.find("{", i)
    if start < 0:
        return None
    depth = 0
    for j in range(start, len(html)):
        c = html[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return html[start : j + 1]
    return None


def parse_youtube(html, url):
    media = []
    raw = _balanced_json(html, "ytInitialPlayerResponse")
    data = None
    if raw:
        try:
            data = json.loads(raw)
        except Exception:
            data = None
    title = _title(html)
    author = _author(html)
    if data:
        details = data.get("videoDetails") or {}
        title = details.get("title") or title
        author = details.get("author") or author
        sd = data.get("streamingData") or {}
        for f in (sd.get("formats") or []) + (sd.get("adaptiveFormats") or []):
            if f.get("url") and str(f.get("mimeType", "")).startswith("video/"):
                q = f.get("qualityLabel") or f.get("quality") or None
                media.append({"url": f["url"], "type": "video", "quality": q})
    if not media:
        vm = re.search(r"[?&]v=([a-zA-Z0-9_-]{6,})|youtu\.be/([a-zA-Z0-9_-]{6,})|shorts/([a-zA-Z0-9_-]{6,})", url)
        vid = vm.group(1) or vm.group(2) or vm.group(3) if vm else None
        if vid:
            media.append({"url": "https://img.youtube.com/vi/%s/maxresdefault.jpg" % vid, "type": "image", "quality": "thumbnail"})
    return {"platform": "youtube", "title": title, "author": author, "media": media}


def parse_instagram(html, url):
    vid = _og(html, "og:video") or _og(html, "og:video:url") or _og(html, "og:video:secure_url")
    img = _og(html, "og:image")
    media = _media(vid, img)
    if not media:
        m = re.search(r'window\._sharedData\s*=\s*(\{.*?\});\s*</script>', html, re.S)
        if m:
            try:
                d = json.loads(m.group(1))
                nodes = (d or {}).get("entry_data", {}).get("PostPage", [{}])[0].get("graphql", {}).get("shortcode_media")
                if nodes and nodes.get("video_url"):
                    media.append({"url": nodes["video_url"], "type": "video"})
                elif nodes and nodes.get("display_url"):
                    media.append({"url": nodes["display_url"], "type": "image"})
            except Exception:
                pass
    return {"platform": "instagram", "title": _title(html), "author": _author(html), "media": media}


def parse_twitter(html, url):
    vid = _og(html, "og:video") or _og(html, "og:video:url") or _og(html, "og:video:secure_url")
    img = _og(html, "og:image")
    media = []
    if vid:
        media.append({"url": vid, "type": "video"})
    for m in re.finditer(r'"url"\s*:\s*"(https://video\.twimg\.com/[^"]+)"', html):
        u = m.group(1)
        if not any(x["url"] == u for x in media):
            media.append({"url": u, "type": "video"})
    if img and not vid:
        media.append({"url": img, "type": "image"})
    return {"platform": "twitter", "title": _title(html), "author": _author(html), "media": media}


def parse_reddit(html, url):
    json_url = url.rstrip("/") + ".json"
    data = None
    req = urllib.request.Request(json_url, headers=_outbound_headers(referer="https://www.reddit.com/"))
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))
    except Exception:
        data = None
    media = []
    if data:
        try:
            post = data[0]["data"]["children"][0]["data"]
            rv = (post.get("media") or {}).get("reddit_video") or {}
            if post.get("is_video") and rv.get("fallback_url"):
                media.append({"url": rv["fallback_url"], "type": "video", "quality": "SD"})
            u = post.get("url") or ""
            if re.search(r"\.(jpe?g|png|gif)(\?|$)", u, re.I):
                media.append({"url": u, "type": "image"})
            if re.search(r"\.gifv?", u, re.I):
                media.append({"url": re.sub(r"\.gifv?", ".mp4", u), "type": "video"})
            return {
                "platform": "reddit",
                "title": post.get("title") or _title(html),
                "author": "u/" + post.get("author", "") if post.get("author") else _author(html),
                "media": media,
            }
        except Exception:
            pass
    return parse_generic(html, url)


def _looks_real_media(url):
    if re.search(r"favicon|apple-touch|logo|shreddit|static\.reddit|/assets/|placeholder", url, re.I):
        return False
    return True


def parse_generic(html, url):
    vid = _og(html, "og:video") or _og(html, "og:video:url") or _og(html, "og:video:secure_url")
    img = _og(html, "og:image") or _og(html, "og:image:url") or _og(html, "twitter:image:src")
    media = []
    if vid and _looks_real_media(vid):
        media.append({"url": vid, "type": "video"})
    if img and _looks_real_media(img):
        media.append({"url": img, "type": "image"})
    if not vid:
        vs = re.search(r'<video[^>]+src="([^"]+)"', html, re.I)
        if vs:
            media.insert(0, {"url": vs.group(1), "type": "video"})
    return {"platform": "generic", "title": _title(html), "author": _author(html), "media": media}


def detect_platform(url):
    if re.search(r"tiktok|vm\.tiktok", url):
        return "tiktok"
    if re.search(r"youtube\.com|youtu\.be", url):
        return "youtube"
    if "instagram" in url:
        return "instagram"
    if re.search(r"twitter\.com|x\.com", url):
        return "twitter"
    if re.search(r"reddit\.com|redd\.it", url):
        return "reddit"
    if re.search(r"pinterest\.|pin\.it", url):
        return "pinterest"
    if re.search(r"facebook\.com|fb\.watch", url):
        return "facebook"
    if "vimeo" in url:
        return "vimeo"
    if "twitch" in url:
        return "twitch"
    return "generic"


PARSERS = {
    "tiktok": parse_tiktok,
    "youtube": parse_youtube,
    "instagram": parse_instagram,
    "twitter": parse_twitter,
    "reddit": parse_reddit,
    "pinterest": parse_generic,
    "facebook": parse_generic,
    "vimeo": parse_generic,
    "twitch": parse_generic,
    "generic": parse_generic,
}


def resolve_url(target_url):
    platform = detect_platform(target_url)
    headers = _outbound_headers()
    if platform == "youtube":
        headers["Cookie"] = "CONSENT=YES+cb.20210720-00-p0.en+FX+999"
    req = urllib.request.Request(target_url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            html = resp.read().decode("utf-8", "ignore")
    except Exception:
        html = ""

    # First choice for platforms that hide/encrypt their streams: yt-dlp.
    # (Only available when running on your own PC — serverless hosts can't run it.)
    if html and platform in ("youtube", "tiktok", "instagram", "twitter"):
        r = resolve_via_ytdlp(target_url, platform)
        if r and r.get("media"):
            return r

    parser = PARSERS.get(platform, parse_generic)
    result = parser(html, target_url)
    # A generic parser may have overwritten the platform name; keep the detected one.
    if result.get("platform") == "generic" and platform != "generic":
        result["platform"] = platform
    result.setdefault("media", [])
    return result


def resolve_via_ytdlp(url, platform):
    """Use the downloads module to extract info via yt-dlp."""
    try:
        info = downloads.media_info(url)
    except Exception:
        info = None
    if not info or not info.get("media"):
        return None
    info["platform"] = platform
    return info


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    # ---------- helpers ----------

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Vary", "Origin, Accept-Encoding")

    def _send(self, code, content_type, body=b""):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _target(self):
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        tgt = (qs.get("url") or [""])[0]
        if not tgt.startswith(("http://", "https://")):
            self._send(400, "text/plain; charset=utf-8", b"Bad URL")
            return None
        return tgt

    # ---------- routes ----------

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
        stripped = path.rstrip("/")
        # accept /api/... (Netlify-style) and classic paths
        api = stripped.startswith("/api")
        route = stripped[4:] if api else stripped

        if route == "/ping":
            self._send(200, "text/plain; charset=utf-8", b"pong")
            return

        if route == "/healthz":
            self._send(200, "text/plain; charset=utf-8", b"ok")
            return

        if route == "/capabilities":
            self._send_json(200, {"ytdlp": downloads.have_ytdlp()})
            return

        if route == "/resolve":
            tgt = self._target()
            if tgt is None:
                return
            try:
                platform = detect_platform(tgt)
                result = resolve_url(tgt)
            except Exception:
                self._send_json(502, {"ok": False, "platform": platform if "platform" in dir() else None, "error": "Could not reach that page."})
                return
            if not result.get("media"):
                self._send_json(404, {"ok": False, "platform": result.get("platform") or platform, "error": "No downloadable media found on that page."})
                return
            result["ok"] = True
            self._send_json(200, result)
            return

        if route == "/dl":
            # Local-only: stream the media via yt-dlp (handles signed/encrypted streams).
            tgt = self._target()
            if tgt is None:
                return
            self._dl(tgt)
            return

        if route == "/proxy":
            tgt = self._target()
            if tgt is None:
                return
            self._proxy(tgt)
            return

        if route in ("", "/index.html"):
            try:
                with open(FILE, "rb") as fh:
                    data = fh.read()
            except OSError:
                self._send(500, "text/plain; charset=utf-8", b"index.html not found")
                return
            self._send(200, "text/html; charset=utf-8", data)
            return

        self._send(404, "text/plain; charset=utf-8", b"Not found")

    def _send_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send(code, "application/json; charset=utf-8", body)

    # ---------- yt-dlp streaming ----------

    def _dl(self, url):
        try:
            gen = downloads.dl_stream(url)
        except downloads.DlError as e:
            self._send(502, "text/plain; charset=utf-8", str(e).encode("utf-8"))
            return
        # Fail fast: if yt-dlp returns nothing at all, don't send a 200+empty body.
        try:
            first = next(gen)
        except StopIteration:
            self._send(502, "text/plain; charset=utf-8", b"Nothing to download")
            return
        except downloads.DlError as e:
            self._send(502, "text/plain; charset=utf-8", str(e).encode("utf-8"))
            return
        except Exception:
            self._send(502, "text/plain; charset=utf-8", b"Download failed")
            return

        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Cache-Control", "no-store")
        self.send_header("x-dl", "yt-dlp")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        chunks = [first]
        try:
            while True:
                chunk = next(gen)
                if chunk:
                    chunks.append(chunk)
                if len(chunks) >= 4:
                    self._flush_chunks(chunks)
                    chunks = []
        except StopIteration:
            pass
        except Exception:
            pass
        finally:
            gen.close()
        if chunks:
            self._flush_chunks(chunks)
        try:
            self.wfile.write(b"0\r\n\r\n")
        except Exception:
            pass

    def _flush_chunks(self, chunks):
        for chunk in chunks:
            self.wfile.write(f"{len(chunk):x}\r\n".encode("ascii"))
            self.wfile.write(chunk)
            self.wfile.write(b"\r\n")
        chunks.clear()

    # ---------- proxying ----------

    def _proxy(self, tgt):
        req = urllib.request.Request(tgt, headers=_outbound_headers(self.headers.get("Range")))
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                code = resp.status
                ctype = resp.headers.get("Content-Type") or "application/octet-stream"
                clen = resp.headers.get("Content-Length")
                crange = resp.headers.get("Content-Range")
                arange = resp.headers.get("Accept-Ranges")
                enc = (resp.headers.get("Content-Encoding") or "").lower()

                if enc == "gzip":
                    import gzip

                    body = gzip.decompress(resp.read())
                    self._proxy_stream(code, ctype, crange, arange, body=body)
                    return

                self.send_response(code)
                self._cors()
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "no-store")
                if crange:
                    self.send_header("Content-Range", crange)
                if arange:
                    self.send_header("Accept-Ranges", arange)
                if clen:
                    self.send_header("Content-Length", clen)
                    self.end_headers()
                    self._stream(resp)
                    return
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                while True:
                    chunk = resp.read(CHUNK)
                    if not chunk:
                        break
                    self.wfile.write(f"{len(chunk):x}\r\n".encode("ascii"))
                    self.wfile.write(chunk)
                    self.wfile.write(b"\r\n")
                self.wfile.write(b"0\r\n\r\n")
        except Exception:
            try:
                self.send_response(502)
                self._cors()
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                body = b"Proxy error"
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception:
                pass

    def _proxy_stream(self, code, ctype, crange, arange, body=b""):
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if crange:
            self.send_header("Content-Range", crange)
        if arange:
            self.send_header("Accept-Ranges", arange)
        self.end_headers()
        self.wfile.write(body)

    def _stream(self, resp):
        while True:
            chunk = resp.read(CHUNK)
            if not chunk:
                break
            self.wfile.write(chunk)


class ThreadedServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8787"))
    print(f"\n  BatSaver running:  http://{host}:{port}/")
    print("  Keep this window open. Close it to stop the tool.\n")
    try:
        ThreadedServer((host, port), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\n  Bye.")