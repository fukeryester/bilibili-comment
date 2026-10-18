# -*- coding: utf-8 -*-
"""读取配置里的 urls，逐条生成评论并写回 message。发送由 bili_comment.py 单独完成。"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "bili_comment_config.json"
AI_DIR = ROOT
MAX_COMMENT_LENGTH = 1000


def unique_urls(items):
    result = []
    seen = set()
    for item in items:
        url = str(item).strip()
        if not url or url in seen:
            continue
        seen.add(url)
        result.append(url)
    return result


def load_urls():
    with CONFIG_PATH.open("r", encoding="utf-8-sig") as handle:
        config = json.load(handle)
    if not isinstance(config, dict):
        raise ValueError("配置文件必须是 JSON 对象")
    configured = config.get("urls")
    if configured is None:
        configured = []
    if isinstance(configured, str):
        configured = [configured]
    if not isinstance(configured, list):
        raise ValueError("urls 必须是字符串数组")
    return config, unique_urls(configured)


def generate_comments(urls):
    sys.path.insert(0, str(AI_DIR))
    from video_comment import comment_video

    comments = []
    for index, url in enumerate(urls, 1):
        print("[%s/%s] 正在阅读 %s" % (index, len(urls), url))
        text = comment_video(url).strip()
        if not text:
            raise RuntimeError("没有生成评论: %s" % url)
        if len(text) > MAX_COMMENT_LENGTH:
            print("评论超过 %s 字，已截断。" % MAX_COMMENT_LENGTH)
            text = text[:MAX_COMMENT_LENGTH].rstrip()
        print(text)
        print("")
        comments.append(text)
    return comments


def save_messages(config, urls, comments):
    config["urls"] = urls
    config["message"] = comments
    with CONFIG_PATH.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(config, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def main():
    if not CONFIG_PATH.is_file():
        print("找不到配置文件: %s" % CONFIG_PATH, file=sys.stderr)
        return 2
    if not (AI_DIR / "video_comment.py").is_file():
        print("找不到 AI 模块: %s" % (AI_DIR / "video_comment.py"), file=sys.stderr)
        return 2

    config, urls = load_urls()
    if not urls:
        print("配置里没有 urls。", file=sys.stderr)
        return 2

    comments = generate_comments(urls)
    save_messages(config, urls, comments)
    print("已把 %s 条评论写入 %s" % (len(comments), CONFIG_PATH))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RuntimeError, ValueError, OSError, ImportError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
