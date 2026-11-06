"""Telegram 推送。只使用标准库, 不需要额外依赖。

配置 (环境变量或 env 文件中的 KEY=VALUE):
    TELEGRAM_BOT_TOKEN   从 @BotFather 获得的机器人 token
    TELEGRAM_CHAT_ID     接收消息的聊天 ID (用 `python -m quantbot telegram-setup` 获取)
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.telegram.org"


def load_env_file(path: str | None) -> dict[str, str]:
    """读取简单的 KEY=VALUE 文件 (忽略空行和 # 注释), 环境变量优先级更低。"""
    values = {k: v for k, v in os.environ.items() if k.startswith("TELEGRAM_")}
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip('"').strip("'")
    return values


class TelegramError(RuntimeError):
    pass


def _call(token: str, method: str, payload: dict | None = None, timeout: float = 20.0) -> dict:
    url = f"{API}/bot{token}/{method}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        # 不在错误信息里带出 token
        raise TelegramError(f"Telegram {method} 失败: HTTP {e.code} {detail}") from None
    except urllib.error.URLError as e:
        raise TelegramError(f"Telegram {method} 网络错误: {e.reason}") from None
    if not body.get("ok"):
        raise TelegramError(f"Telegram {method} 返回错误: {body.get('description')}")
    return body["result"]


def send_telegram(token: str, chat_id: str, text: str) -> None:
    if not token or not chat_id:
        raise TelegramError("缺少 TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID")
    # Telegram 单条消息上限 4096 字符
    _call(token, "sendMessage", {"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": True})


def find_chat_ids(token: str) -> list[tuple[str, str]]:
    """返回最近给机器人发过消息的聊天: [(chat_id, 名称)]。"""
    updates = _call(token, "getUpdates")
    seen: dict[str, str] = {}
    for u in updates:
        msg = u.get("message") or u.get("channel_post") or {}
        chat = msg.get("chat") or {}
        if "id" in chat:
            name = chat.get("title") or " ".join(x for x in (chat.get("first_name"), chat.get("last_name")) if x)
            seen[str(chat["id"])] = name or chat.get("username") or ""
    return list(seen.items())
