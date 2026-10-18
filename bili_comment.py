# -*- coding: utf-8 -*-
"""用 HTTP 接口向哔哩哔哩视频或音频发送同一条评论。

配置文件里的 urls 是数组。message 是字符串时，同一句发给每一个链接；
message 是数组时，按 urls 的顺序逐条对应。
仍兼容旧字段 url，以及重复传入的 --url。

用法：
  1. 复制 bili_comment_config.example.json 为 bili_comment_config.json
  2. 填入自己账号的 Cookie、目标链接和评论内容
  3. python bili_comment.py
     python bili_comment.py --dry-run
     python bili_comment.py --url "https://www.bilibili.com/video/BVxxxx" --url "https://www.bilibili.com/audio/au123" --message "你好"

Cookie 从自己已登录的浏览器里复制，至少要有 SESSDATA 和 bili_jct。
SESSDATA 等同于登录凭证，不要发给别人，也不要提交到仓库。

请使用自己的账号，并遵守哔哩哔哩的使用条款。
"""

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API_VIEW = "https://api.bilibili.com/x/web-interface/view"
API_REPLY_ADD = "https://api.bilibili.com/x/v2/reply/add"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

TYPE_VIDEO = 1
TYPE_AUDIO = 14
DEFAULT_INTERVAL_SECONDS = 2
STOP_CODES = {-101, -111, 12015}

CONFIG_PATH = Path(__file__).with_name("bili_comment_config.json")

BV_RE = re.compile(r"(?:/video/|bvid=)(BV[0-9A-Za-z]+)", re.IGNORECASE)
AV_RE = re.compile(r"(?:/video/av|aid=|avid=)(\d+)", re.IGNORECASE)
AU_RE = re.compile(r"(?:/audio/au|auid=)(\d+)", re.IGNORECASE)


def load_config(path):
    if not path.is_file():
        return {}
    with path.open("r", encoding="utf-8-sig") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("配置文件必须是 JSON 对象")
    return data


def cookie_value(cookie, name):
    for part in cookie.split(";"):
        item = part.strip()
        if not item or "=" not in item:
            continue
        key, value = item.split("=", 1)
        if key.strip() == name:
            return value.strip()
    return ""


def request_json(url, data=None, headers=None, timeout=20):
    req_headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if headers:
        req_headers.update(headers)
    body = None
    if data is not None:
        body = urllib.parse.urlencode(data).encode("utf-8")
        req_headers["Content-Type"] = "application/x-www-form-urlencoded"
    request = urllib.request.Request(url, data=body, headers=req_headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            final_url = response.geturl()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError("HTTP %s: %s" % (exc.code, detail[:500])) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError("网络请求失败: %s" % exc.reason) from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("接口返回的不是 JSON: %s" % raw[:300]) from exc
    return payload, final_url


def follow_redirect(url):
    """短链（如 b23.tv）先跳转到真实页面，再解析编号。"""
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.geturl()
    except urllib.error.HTTPError as exc:
        if exc.geturl():
            return exc.geturl()
        raise RuntimeError("打开链接失败: HTTP %s" % exc.code) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError("打开链接失败: %s" % exc.reason) from exc


def aid_from_bvid(bvid):
    payload, _ = request_json("%s?bvid=%s" % (API_VIEW, urllib.parse.quote(bvid)))
    if payload.get("code") != 0:
        raise RuntimeError(
            "查询视频失败: code=%s message=%s"
            % (payload.get("code"), payload.get("message"))
        )
    aid = (payload.get("data") or {}).get("aid")
    if not aid:
        raise RuntimeError("查询视频成功，但响应里没有 aid")
    title = (payload.get("data") or {}).get("title") or ""
    return int(aid), title


def parse_target(url):
    matched_audio = AU_RE.search(url)
    if matched_audio:
        return TYPE_AUDIO, int(matched_audio.group(1)), ""

    matched_av = AV_RE.search(url)
    if matched_av:
        return TYPE_VIDEO, int(matched_av.group(1)), ""

    matched_bv = BV_RE.search(url)
    if matched_bv:
        aid, title = aid_from_bvid(matched_bv.group(1))
        return TYPE_VIDEO, aid, title

    raise RuntimeError(
        "无法从链接识别目标。支持的形式："
        "https://www.bilibili.com/video/BVxxxx 、"
        "https://www.bilibili.com/video/av123 、"
        "https://www.bilibili.com/audio/au123 ，"
        "以及会跳到这些页面的 b23.tv 短链。"
    )


def send_comment(target_type, oid, message, cookie, csrf, referer):
    payload, _ = request_json(
        API_REPLY_ADD,
        data={
            "type": str(target_type),
            "oid": str(oid),
            "message": message,
            "plat": "1",
            "csrf": csrf,
        },
        headers={
            "Cookie": cookie,
            "Referer": referer,
            "Origin": "https://www.bilibili.com",
        },
    )
    return payload


def type_name(target_type):
    if target_type == TYPE_VIDEO:
        return "视频"
    if target_type == TYPE_AUDIO:
        return "音频"
    return str(target_type)


def build_parser():
    parser = argparse.ArgumentParser(description="向哔哩哔哩的多个视频或音频发送同一条评论")
    parser.add_argument("--config", default=str(CONFIG_PATH), help="JSON 配置文件路径")
    parser.add_argument(
        "--url",
        action="append",
        default=[],
        help="目标链接，可重复传入。传入后忽略配置文件里的 urls 和 url",
    )
    parser.add_argument("--message", default="", help="评论内容，覆盖配置文件")
    parser.add_argument("--cookie", default="", help="浏览器 Cookie，覆盖配置文件")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只解析链接并打印将要提交的参数，不发送评论",
    )
    return parser


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


def collect_urls(args, config):
    if args.url:
        return unique_urls(args.url)

    items = []
    configured = config.get("urls")
    if configured is None:
        configured = []
    if isinstance(configured, str):
        configured = [configured]
    if not isinstance(configured, list):
        raise ValueError("urls 必须是字符串数组")
    items.extend(configured)

    single = config.get("url")
    if isinstance(single, str) and single.strip():
        items.append(single)
    return unique_urls(items)


def interval_seconds(config):
    raw = config.get("interval_seconds", DEFAULT_INTERVAL_SECONDS)
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("interval_seconds 必须是数字") from exc
    if value < 0:
        raise ValueError("interval_seconds 不能小于 0")
    return value


def resolve_page(url):
    final_url = url
    host = urllib.parse.urlparse(url).netloc
    if "b23.tv" in host:
        final_url = follow_redirect(url)
        print("短链跳转到: %s" % final_url)
    target_type, oid, title = parse_target(final_url)
    return final_url, target_type, oid, title


def explain_failure(result):
    code = result.get("code")
    if code in (-101, -111):
        print("登录态无效或 csrf 不匹配。请重新从浏览器复制 SESSDATA 和 bili_jct。", file=sys.stderr)
    elif (result.get("data") or {}).get("need_captcha"):
        print("账号触发了验证码。请先在网页里正常发一条评论，通过后再用脚本。", file=sys.stderr)
    elif code == 12015:
        print("评论发送过快，已停止后续链接。", file=sys.stderr)


def expand_messages(raw, count):
    """字符串时每条链接用同一句；数组时按 urls 的顺序逐条对应。"""
    if isinstance(raw, list):
        messages = []
        for item in raw:
            text = str(item).strip()
            if not text:
                return [], "message 数组里有空评论。"
            if len(text) > 1000:
                return [], "评论最长 1000 个字符，当前长度为 %s。" % len(text)
            messages.append(text)
        if len(messages) != count:
            return [], "message 有 %s 条，urls 有 %s 个，数量需要一致。" % (len(messages), count)
        return messages, ""

    message = "" if raw is None else str(raw).strip()
    if not message:
        return [], ""
    if len(message) > 1000:
        return [], "评论最长 1000 个字符，当前长度为 %s。" % len(message)
    return [message] * count, ""


def main(argv):
    args = build_parser().parse_args(argv)
    config = load_config(Path(args.config))

    urls = collect_urls(args, config)
    cookie = (args.cookie or config.get("cookie") or "").strip()
    pause = interval_seconds(config)

    if not urls:
        print("缺少目标链接。请在配置文件填写 urls 数组，或重复传入 --url。", file=sys.stderr)
        return 2
    if args.message:
        messages, message_error = expand_messages(args.message, len(urls))
    else:
        messages, message_error = expand_messages(config.get("message"), len(urls))
    if message_error:
        print(message_error, file=sys.stderr)
        return 2
    if not messages:
        print("缺少评论内容。请在配置文件的 message 字段填写，或使用 --message。", file=sys.stderr)
        return 2

    same_message = len(set(messages)) == 1
    if same_message:
        print("评论内容: %s" % messages[0])
    else:
        print("评论内容: 按链接分别发送")
    print("目标数量: %s" % len(urls))

    targets = []
    resolve_failed = 0
    for index, url in enumerate(urls, 1):
        print("[%s/%s] 解析 %s" % (index, len(urls), url))
        try:
            final_url, target_type, oid, title = resolve_page(url)
        except RuntimeError as exc:
            resolve_failed += 1
            print("解析失败: %s" % exc, file=sys.stderr)
            continue
        print("目标类型: %s" % type_name(target_type))
        print("目标 oid: %s" % oid)
        if title:
            print("稿件标题: %s" % title)
        if not same_message:
            print("本条评论: %s" % messages[index - 1])
        targets.append((final_url, target_type, oid, messages[index - 1]))

    if args.dry_run:
        print("dry-run：未发送评论。可发送 %s 个，解析失败 %s 个。" % (len(targets), resolve_failed))
        return 0 if resolve_failed == 0 else 1

    if not targets:
        print("没有可以发送的目标。", file=sys.stderr)
        return 1

    if not cookie:
        print(
            "缺少 Cookie。请在配置文件填写自己浏览器里的 SESSDATA 和 bili_jct。",
            file=sys.stderr,
        )
        return 2

    csrf = cookie_value(cookie, "bili_jct")
    sessdata = cookie_value(cookie, "SESSDATA")
    if not csrf or not sessdata:
        print("Cookie 里需要同时包含 SESSDATA 和 bili_jct。", file=sys.stderr)
        return 2

    success = 0
    failed = resolve_failed
    skipped = 0
    for index, (final_url, target_type, oid, comment_text) in enumerate(targets, 1):
        if index > 1 and pause:
            time.sleep(pause)
        print("[%s/%s] 发送到 oid=%s" % (index, len(targets), oid))
        if not same_message:
            print("本条评论: %s" % comment_text)
        result = send_comment(target_type, oid, comment_text, cookie, csrf, final_url)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        code = result.get("code")
        if code == 0:
            rpid = ((result.get("data") or {}).get("rpid_str")) or ((result.get("data") or {}).get("rpid"))
            print("发送成功，rpid=%s" % rpid)
            success += 1
            continue

        failed += 1
        print(
            "发送失败: code=%s message=%s" % (code, result.get("message")),
            file=sys.stderr,
        )
        explain_failure(result)
        if code in STOP_CODES or (result.get("data") or {}).get("need_captcha"):
            skipped = len(targets) - index
            if skipped:
                print("剩余 %s 个链接未发送。" % skipped, file=sys.stderr)
            break

    print("完成：成功 %s，失败 %s，未发送 %s。" % (success, failed, skipped))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except (RuntimeError, ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
