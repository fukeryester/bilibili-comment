# -*- coding: utf-8 -*-
"""从哔哩哔哩公开列表里随机抽一个视频，并把地址写入配置的 urls。

用法：
  python random_bili_video.py
  python random_bili_video.py --config 其他配置.json
"""

import argparse
import json
import random
import sys
import urllib.error
import urllib.request
from pathlib import Path

CONFIG_PATH = Path(__file__).with_name("bili_comment_config.json")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
RANKING_URL = "https://api.bilibili.com/x/web-interface/ranking/v2?rid=0&type=all"
POPULAR_URL = "https://api.bilibili.com/x/web-interface/popular?ps=20&pn=%s"


def request_json(url):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Referer": "https://www.bilibili.com/",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError("HTTP %s: %s" % (exc.code, detail[:300])) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError("网络请求失败: %s" % exc.reason) from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("接口返回的不是 JSON") from exc
    if payload.get("code") != 0:
        raise RuntimeError(
            "接口失败: code=%s message=%s" % (payload.get("code"), payload.get("message"))
        )
    return payload


def videos_from_payload(payload):
    data = payload.get("data") or {}
    items = data.get("list") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    videos = []
    for item in items:
        if not isinstance(item, dict):
            continue
        bvid = str(item.get("bvid") or "").strip()
        aid = item.get("aid")
        if not bvid.startswith("BV") or aid in (None, ""):
            continue
        try:
            aid = int(aid)
        except (TypeError, ValueError):
            continue
        videos.append(
            {
                "aid": aid,
                "bvid": bvid,
                "title": str(item.get("title") or "").strip(),
                "url": "https://www.bilibili.com/video/%s" % bvid,
            }
        )
    return videos


def find_random_video():
    """随机返回一个公开视频，包含 aid、bvid、title、url。"""
    candidates = []
    seen = set()
    sources = [
        RANKING_URL,
        POPULAR_URL % random.randint(1, 5),
    ]
    for url in sources:
        try:
            payload = request_json(url)
        except (RuntimeError, OSError, ValueError):
            continue
        for video in videos_from_payload(payload):
            if video["bvid"] in seen:
                continue
            seen.add(video["bvid"])
            candidates.append(video)
    if not candidates:
        raise RuntimeError("没有从哔哩哔哩公开列表里找到可用视频")
    return random.choice(candidates)


def fill_config(config_path=None):
    """抽一个视频，并用它的页面地址覆盖配置里的 urls。其他字段保持不变。"""
    path = Path(config_path) if config_path else CONFIG_PATH
    if not path.is_file():
        raise RuntimeError("找不到配置文件: %s" % path)
    with path.open("r", encoding="utf-8-sig") as handle:
        config = json.load(handle)
    if not isinstance(config, dict):
        raise ValueError("配置文件必须是 JSON 对象")

    video = find_random_video()
    config["urls"] = [video["url"]]
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(config, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return video


def build_parser():
    parser = argparse.ArgumentParser(description="随机抽取一个哔哩哔哩视频并写入配置 urls")
    parser.add_argument("--config", default=str(CONFIG_PATH), help="JSON 配置文件路径")
    return parser


def main(argv):
    args = build_parser().parse_args(argv)
    video = fill_config(args.config)
    print("AV: av%s" % video["aid"])
    print("BV: %s" % video["bvid"])
    if video["title"]:
        print("标题: %s" % video["title"])
    print("已写入 urls: %s" % video["url"])
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except (RuntimeError, ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
