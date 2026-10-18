# -*- coding: utf-8 -*-
"""给定网页 URL，观察其中的视频并返回一段评论。

DeepSeek 接口接受文字和图片，不接受视频文件。本模块会读取网页上的标题、
简介和视频地址；本机有 ffmpeg 时按时间抽帧，有 yt-dlp 时解析视频站播放地址。
抽到的画面和页面文字一起发给模型。

系统提示词固定为「观察这个网页里面的视频，做一定的总结并且写一段评论」。

未传入 api_key、也没有环境变量时，从同目录配置文件的 deepseek_api_key 读取。

用法::

    from video_comment import comment_video

    text = comment_video("https://example.com/watch")
"""

from __future__ import annotations

import base64
import gzip
import html
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zlib
from html.parser import HTMLParser
from pathlib import Path

SYSTEM_PROMPT = "观察这个网页里面的视频，做一定的总结并且写一段评论"
_DEFAULT_API_KEY = ""

_API_URL = "https://api.deepseek.com/chat/completions"
_MODEL = "deepseek-flash"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)
_MEDIA_EXTS = (".mp4", ".webm", ".mov", ".m4v", ".mkv", ".m3u8", ".flv")
_BV_RE = re.compile(r"(?:/video/|bvid=)(BV[0-9A-Za-z]+)", re.IGNORECASE)
_AU_RE = re.compile(r"(?:/audio/au|auid=)(\d+)", re.IGNORECASE)
_FRAME_RATIOS = (0.12, 0.35, 0.58, 0.82)
_MAX_PAGE_BYTES = 2_000_000
_MAX_IMAGE_BYTES = 2_000_000


class _PageInfo:
    def __init__(self) -> None:
        self.title = ""
        self.description = ""
        self.media_urls: list[str] = []
        self.image_urls: list[str] = []
        self.player_urls: list[str] = []
        self.text = ""


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.metas: list[tuple[str, str]] = []
        self.media_urls: list[str] = []
        self.image_urls: list[str] = []
        self.player_urls: list[str] = []
        self.text_parts: list[str] = []
        self._in_title = False
        self._in_video = 0
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {key.lower(): value or "" for key, value in attrs}
        if tag in ("script", "style", "noscript"):
            self._skip += 1
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            key = (attr.get("property") or attr.get("name") or "").lower()
            content = attr.get("content", "").strip()
            if key and content:
                self.metas.append((key, content))
        elif tag == "video":
            self._in_video += 1
            self._keep_media(attr.get("src", ""))
            self._keep_image(attr.get("poster", ""))
        elif tag == "source" and self._in_video:
            self._keep_media(attr.get("src", ""))
        elif tag == "iframe":
            self._keep_player(attr.get("src", ""))
        elif tag == "img":
            src = attr.get("src") or attr.get("data-src") or ""
            if _meta_image_hint(attr.get("alt", ""), attr.get("class", "")):
                self._keep_image(src)

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        elif tag == "video" and self._in_video:
            self._in_video -= 1
        elif tag in ("script", "style", "noscript") and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
        elif self._skip == 0:
            text = data.strip()
            if text:
                self.text_parts.append(text)

    def _keep_media(self, url: str) -> None:
        url = url.strip()
        if url and not url.startswith("data:"):
            self.media_urls.append(url)

    def _keep_image(self, url: str) -> None:
        url = url.strip()
        if url and not url.startswith("data:"):
            self.image_urls.append(url)

    def _keep_player(self, url: str) -> None:
        url = url.strip()
        if url and not url.startswith("data:"):
            self.player_urls.append(url)


def _configured_api_key() -> str:
    path = Path(__file__).resolve().with_name("bili_comment_config.json")
    if not path.is_file():
        return ""
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(data, dict):
        return ""
    return str(data.get("deepseek_api_key") or "").strip()


def comment_video(page_url: str, api_key: str | None = None) -> str:
    """打开网页，把视频相关内容和画面交给 DeepSeek，返回一段评论。"""
    if not page_url or not str(page_url).strip():
        raise ValueError("page_url 不能为空")
    page_url = str(page_url).strip()
    key = (
        api_key
        or os.environ.get("DEEPSEEK_API_KEY")
        or _configured_api_key()
        or _DEFAULT_API_KEY
    ).strip()
    if not key:
        raise RuntimeError("缺少 DeepSeek API Key")

    info, media_url = _collect(page_url)
    frames = _extract_frames(media_url, page_url) if media_url else []
    images: list[tuple[str, bytes]] = [("image/jpeg", frame) for frame in frames]
    if not images:
        for image_url in info.image_urls[:2]:
            downloaded = _download_image(image_url, page_url)
            if downloaded is not None:
                images.append(downloaded)

    user_text = _build_user_text(
        page_url,
        info,
        media_url,
        has_frames=bool(frames),
        has_images=bool(images),
    )
    return _ask_deepseek(key, user_text, images)


def _collect(page_url: str) -> tuple[_PageInfo, str]:
    if _is_media_url(page_url):
        info = _PageInfo()
        info.media_urls = [page_url]
        return info, page_url

    final_url, page_html, is_video = _fetch_page(page_url)
    if is_video:
        info = _PageInfo()
        info.media_urls = [final_url]
        return info, final_url

    info = _parse_html(page_html, final_url)
    _apply_bilibili(info, final_url)
    media_url = _pick_media_url(info.media_urls)
    if not media_url:
        media_url = _resolve_with_ytdlp(final_url)
    return info, media_url


def _fetch_page(url: str) -> tuple[str, str, bool]:
    headers = {
        "User-Agent": _UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    try:
        with _open_url(url, headers, 25) as resp:
            final_url = resp.geturl()
            content_type = (resp.headers.get("Content-Type") or "").lower()
            if content_type.startswith("video/"):
                return final_url, "", True
            raw = _read_limited(resp, _MAX_PAGE_BYTES)
            raw = _decompress(raw, resp.headers.get("Content-Encoding"))
            charset = resp.headers.get_content_charset() or "utf-8"
    except urllib.error.URLError as exc:
        raise RuntimeError("打开网页失败：{0}".format(exc)) from exc
    return final_url, raw.decode(charset, "replace"), False


def _parse_html(page_html: str, base_url: str) -> _PageInfo:
    parser = _PageParser()
    try:
        parser.feed(page_html)
        parser.close()
    except Exception:
        pass

    info = _PageInfo()
    title_text = _clean_text(" ".join(parser.title_parts))
    og_title = ""
    twitter_title = ""
    og_description = ""
    plain_description = ""
    for key, content in parser.metas:
        value = _clean_text(html.unescape(content))
        absolute = _abs_url(base_url, content.strip())
        if key == "og:title" and value:
            og_title = value
        elif key == "twitter:title" and value and not twitter_title:
            twitter_title = value
        elif key == "og:description" and value and not og_description:
            og_description = value
        elif key in ("description", "twitter:description") and value and not plain_description:
            plain_description = value
        elif key in ("og:video", "og:video:url", "og:video:secure_url"):
            if _is_media_url(absolute):
                info.media_urls.append(absolute)
            elif absolute:
                info.player_urls.append(absolute)
        elif key in ("og:image", "og:image:url", "twitter:image"):
            info.image_urls.append(absolute)

    info.title = og_title or twitter_title or title_text
    info.description = og_description or plain_description
    for url in parser.media_urls:
        info.media_urls.append(_abs_url(base_url, url))
    for url in parser.image_urls:
        info.image_urls.append(_abs_url(base_url, url))
    for url in parser.player_urls:
        info.player_urls.append(_abs_url(base_url, url))

    info.media_urls = _dedupe(info.media_urls)
    info.image_urls = _dedupe(info.image_urls)
    info.player_urls = _dedupe(info.player_urls)
    info.text = _clean_text(" ".join(parser.text_parts))[:1800]
    return info


def _apply_bilibili(info: _PageInfo, page_url: str) -> None:
    """哔哩哔哩页面正文是压缩的，稿件信息改从接口读取，避免把乱码交给模型。"""
    matched_video = _BV_RE.search(page_url)
    if matched_video:
        _apply_bilibili_video(info, matched_video.group(1))
        return
    matched_audio = _AU_RE.search(page_url)
    if matched_audio:
        _apply_bilibili_audio(info, matched_audio.group(1))


def _apply_bilibili_video(info: _PageInfo, bvid: str) -> None:
    payload = _get_json(
        "https://api.bilibili.com/x/web-interface/view?bvid={0}".format(urllib.parse.quote(bvid))
    )
    if not payload:
        return
    data = payload.get("data") or {}
    if not isinstance(data, dict):
        return
    title = str(data.get("title") or "").strip()
    owner = str((data.get("owner") or {}).get("name") or "").strip()
    partition = str(data.get("tname") or "").strip()
    duration = _format_duration(data.get("duration"))
    cover = _https_url(str(data.get("pic") or ""))
    if title:
        info.title = title
    info.description = _meaningful_text(str(data.get("desc") or ""))
    lines: list[str] = []
    if owner:
        lines.append("作者：{0}".format(owner))
    if partition:
        lines.append("分区：{0}".format(partition))
    if duration:
        lines.append("时长：{0}".format(duration))
    tags = _bilibili_tags(bvid)
    if tags:
        lines.append("标签：{0}".format("、".join(tags)))
    stat_line = _stat_line(data.get("stat") or {})
    if stat_line:
        lines.append(stat_line)
    info.text = "\n".join(lines)
    if cover:
        info.image_urls = [cover]


def _apply_bilibili_audio(info: _PageInfo, sid: str) -> None:
    payload = _get_json(
        "https://api.bilibili.com/audio/music-service-c/web/song/info?sid={0}".format(sid)
    )
    if not payload:
        return
    data = payload.get("data") or {}
    if not isinstance(data, dict):
        return
    title = str(data.get("title") or "").strip()
    author = str(data.get("author") or data.get("uname") or "").strip()
    intro = _meaningful_text(str(data.get("intro") or ""))
    lyric = _meaningful_text(str(data.get("lyric") or ""))
    duration = _format_duration(data.get("duration"))
    cover = _https_url(str(data.get("cover") or ""))
    if title:
        info.title = title
    info.description = intro
    lines: list[str] = []
    if author:
        lines.append("作者：{0}".format(author))
    if duration:
        lines.append("时长：{0}".format(duration))
    if lyric:
        lines.append("歌词：{0}".format(lyric[:800]))
    info.text = "\n".join(lines)
    if cover:
        info.image_urls = [cover]


def _bilibili_tags(bvid: str) -> list[str]:
    payload = _get_json(
        "https://api.bilibili.com/x/tag/archive/tags?bvid={0}".format(urllib.parse.quote(bvid))
    )
    if not payload:
        return []
    names: list[str] = []
    for item in payload.get("data") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("tag_name") or "").strip()
        if name:
            names.append(name)
    return names[:12]


def _stat_line(stat: dict) -> str:
    labels = (
        ("view", "播放"),
        ("danmaku", "弹幕"),
        ("like", "点赞"),
        ("coin", "投币"),
        ("favorite", "收藏"),
        ("share", "转发"),
    )
    parts: list[str] = []
    for key, label in labels:
        value = stat.get(key)
        if isinstance(value, int) and value >= 0:
            parts.append("{0} {1}".format(label, value))
    return "，".join(parts)


def _format_duration(raw: object) -> str:
    try:
        seconds = int(raw)
    except (TypeError, ValueError):
        return ""
    if seconds <= 0:
        return ""
    if seconds < 60:
        return "{0}秒".format(seconds)
    minutes, remain = divmod(seconds, 60)
    if minutes < 60:
        return "{0}分{1}秒".format(minutes, remain)
    hours, minutes = divmod(minutes, 60)
    return "{0}小时{1}分{2}秒".format(hours, minutes, remain)


def _meaningful_text(text: str) -> str:
    cleaned = text.strip()
    if cleaned in ("", ".", "。", "无", "暂无简介"):
        return ""
    return cleaned


def _https_url(url: str) -> str:
    url = url.strip()
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("http://"):
        return "https://" + url[len("http://"):]
    return url


def _get_json(url: str) -> dict | None:
    headers = {
        "User-Agent": _UA,
        "Referer": "https://www.bilibili.com/",
        "Accept": "application/json,text/plain,*/*",
    }
    try:
        with _open_url(url, headers, 20) as resp:
            raw = _read_limited(resp, _MAX_PAGE_BYTES)
            raw = _decompress(raw, resp.headers.get("Content-Encoding"))
    except (urllib.error.URLError, OSError, ValueError):
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("code") != 0:
        return None
    return payload


def _decompress(raw: bytes, content_encoding: str | None) -> bytes:
    encoding = (content_encoding or "").lower()
    try:
        if "gzip" in encoding or raw[:2] == b"\x1f\x8b":
            return gzip.decompress(raw)
        if "deflate" in encoding:
            return zlib.decompress(raw)
    except (OSError, EOFError, zlib.error):
        return raw
    return raw


def _build_user_text(
    page_url: str,
    info: _PageInfo,
    media_url: str,
    has_frames: bool,
    has_images: bool,
) -> str:
    lines = [
        "网页地址：{0}".format(page_url),
        "页面标题：{0}".format(info.title or "（无）"),
        "页面简介：{0}".format(info.description or "（无）"),
    ]
    if media_url:
        lines.append("视频地址：{0}".format(media_url))
    if info.player_urls:
        lines.append("播放器地址：{0}".format("；".join(info.player_urls[:3])))
    if info.text:
        lines.append("页面文字摘录：{0}".format(info.text))
    if has_frames:
        lines.append("下面几张图是从该网页视频中按时间顺序截取的画面。")
    elif has_images:
        lines.append("未能读取视频播放画面，下面是该网页上的视频封面或配图。")
    else:
        lines.append("未能读取视频画面或封面，请只根据以上网页文字信息评论。")
    lines.append("请只根据上面给出的信息写一段中文评论，不要编造未提供的情节。")
    return "\n".join(lines)


def _ask_deepseek(api_key: str, user_text: str, images: list[tuple[str, bytes]]) -> str:
    content: list[dict] = [{"type": "text", "text": user_text}]
    for mime, blob in images:
        encoded = base64.b64encode(blob).decode("ascii")
        content.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": "data:{0};base64,{1}".format(mime, encoded),
                    "detail": "low",
                },
            }
        )
    payload = {
        "model": _MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ],
        "temperature": 0.7,
        "max_tokens": 800,
        "stream": False,
        "thinking": {"type": "disabled"},
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        _API_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer {0}".format(api_key),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise RuntimeError("DeepSeek 请求失败：HTTP {0} {1}".format(exc.code, detail)) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError("DeepSeek 请求失败：{0}".format(exc)) from exc

    try:
        data = json.loads(raw.decode("utf-8"))
        message = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("DeepSeek 返回无法解析") from exc

    text = _message_text(message.get("content"))
    if not text:
        raise RuntimeError("DeepSeek 没有返回评论文本")
    return text


def _message_text(content: object) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                value = item.get("text") or item.get("content") or ""
                if isinstance(value, str):
                    parts.append(value)
        return "\n".join(part.strip() for part in parts if part and part.strip()).strip()
    return ""


def _extract_frames(video_url: str, page_url: str) -> list[bytes]:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not video_url:
        return []
    duration = _probe_duration(video_url, page_url)
    if duration and duration > 1:
        times = [max(0.3, duration * ratio) for ratio in _FRAME_RATIOS]
    else:
        times = [1, 5, 12, 24]

    frames: list[bytes] = []
    with tempfile.TemporaryDirectory(prefix="video_comment_") as folder:
        for index, moment in enumerate(times):
            output = os.path.join(folder, "frame_{0}.jpg".format(index))
            if not _grab_frame(ffmpeg, video_url, page_url, moment, output):
                continue
            try:
                with open(output, "rb") as handle:
                    blob = handle.read()
            except OSError:
                continue
            if blob and _image_mime(blob) == "image/jpeg":
                frames.append(blob)
            if len(frames) >= 4:
                break
    return frames


def _grab_frame(ffmpeg: str, video_url: str, page_url: str, moment: float, output: str) -> bool:
    command = [
        ffmpeg,
        "-y",
        "-loglevel",
        "error",
        "-user_agent",
        _UA,
        "-headers",
        "Referer: {0}\r\n".format(page_url),
        "-ss",
        "{0:.2f}".format(moment),
        "-i",
        video_url,
        "-frames:v",
        "1",
        "-vf",
        "scale=640:-2",
        "-q:v",
        "5",
        output,
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            timeout=25,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return result.returncode == 0 and os.path.isfile(output)


def _probe_duration(video_url: str, page_url: str) -> float | None:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    command = [
        ffprobe,
        "-v",
        "error",
        "-user_agent",
        _UA,
        "-headers",
        "Referer: {0}\r\n".format(page_url),
        "-show_entries",
        "format=duration",
        "-of",
        "csv=p=0",
        video_url,
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            timeout=20,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    text = result.stdout.decode("utf-8", "replace").strip()
    try:
        duration = float(text)
    except ValueError:
        return None
    if duration <= 0 or duration > 12 * 60 * 60:
        return None
    return duration


def _resolve_with_ytdlp(page_url: str) -> str:
    executable = shutil.which("yt-dlp") or shutil.which("yt-dlp.exe")
    if not executable:
        return ""
    command = [
        executable,
        "--no-playlist",
        "--no-warnings",
        "--quiet",
        "-g",
        "-f",
        "b[height<=720][ext=mp4]/b[height<=720]/b",
        page_url,
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            timeout=45,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return ""
    if result.returncode != 0:
        return ""
    for line in result.stdout.decode("utf-8", "replace").splitlines():
        candidate = line.strip()
        if candidate.startswith("http://") or candidate.startswith("https://"):
            return candidate
    return ""


def _download_image(image_url: str, page_url: str) -> tuple[str, bytes] | None:
    headers = {
        "User-Agent": _UA,
        "Referer": page_url,
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }
    try:
        with _open_url(image_url, headers, 20) as resp:
            blob = _read_limited(resp, _MAX_IMAGE_BYTES + 1)
    except (urllib.error.URLError, OSError, ValueError):
        return None
    if not blob or len(blob) > _MAX_IMAGE_BYTES:
        return None
    mime = _image_mime(blob)
    if not mime:
        return None
    return mime, blob


def _open_url(url: str, headers: dict[str, str], timeout: int):
    request = urllib.request.Request(url, headers=headers, method="GET")
    return urllib.request.urlopen(request, timeout=timeout)


def _read_limited(resp: object, limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while total < limit:
        chunk = resp.read(min(65536, limit - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


def _image_mime(blob: bytes) -> str:
    if blob.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if blob.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if blob.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(blob) >= 12 and blob.startswith(b"RIFF") and blob[8:12] == b"WEBP":
        return "image/webp"
    return ""


def _is_media_url(url: str) -> bool:
    path = urllib.parse.urlparse(url).path.lower()
    return path.endswith(_MEDIA_EXTS)


def _pick_media_url(urls: list[str]) -> str:
    ranked: list[tuple[int, str]] = []
    for url in urls:
        if not _is_media_url(url):
            continue
        path = urllib.parse.urlparse(url).path.lower()
        if path.endswith(".mp4"):
            score = 3
        elif path.endswith((".webm", ".mov", ".m4v", ".mkv")):
            score = 2
        else:
            score = 1
        ranked.append((score, url))
    if not ranked:
        return ""
    ranked.sort(key=lambda item: item[0], reverse=True)
    return ranked[0][1]


def _abs_url(base_url: str, url: str) -> str:
    url = html.unescape(url.strip())
    if not url or url.startswith("data:"):
        return ""
    return urllib.parse.urljoin(base_url, url)


def _dedupe(urls: list[str]) -> list[str]:
    seen = set()
    result: list[str] = []
    for url in urls:
        if not url or url in seen:
            continue
        seen.add(url)
        result.append(url)
    return result


def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _meta_image_hint(alt: str, css_class: str) -> bool:
    hint = "{0} {1}".format(alt, css_class).lower()
    return any(word in hint for word in ("cover", "poster", "thumb", "video"))


def main() -> None:
    import sys

    if len(sys.argv) < 2:
        print("用法: python video_comment.py <网页URL>", file=sys.stderr)
        raise SystemExit(2)
    print(comment_video(sys.argv[1]))


if __name__ == "__main__":
    main()
