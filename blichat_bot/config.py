from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import List


_DOTENV_LOADED = False


def _load_dotenv(path: str = ".env") -> None:
    """从项目根目录加载 .env 到当前进程的环境变量。

    - 不会覆盖已经存在于 os.environ 的变量。
    - 忽略空行和以 # 开头的注释行。
    """

    global _DOTENV_LOADED

    if _DOTENV_LOADED:
        return

    _DOTENV_LOADED = True

    env_path = Path(path)
    if not env_path.is_file():
        return

    try:
        content = env_path.read_text(encoding="utf-8")
    except OSError:
        return

    for line in content.splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue
        if "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        key = key.strip()
        if not key:
            continue
        # 去掉包裹在引号里的值
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


@dataclass
class Config:
    """全局配置，从环境变量 / .env 中读取。

    必填：
    - BILI_ROOM_ID: 直播间真实房间号（整数）
    - BILI_SESSDATA, BILI_BILI_JCT: 用于代表你账号身份发弹幕的 Cookie 片段
    - TG_BOT_TOKEN: Telegram Bot 的 token
    - TG_CHAT_ID: 接收弹幕的 Telegram chat id（通常是你自己的 user id）
    """

    bili_room_id: int
    bili_sessdata: str
    bili_bili_jct: str
    bili_buvid3: str | None

    tg_bot_token: str
    tg_chat_id: int
    tg_allowed_user_ids: List[int]

    @classmethod
    def from_env(cls) -> "Config":
        # 先尝试从 .env 加载，再读环境变量
        _load_dotenv()

        def require_env(name: str) -> str:
            value = os.getenv(name)
            if not value:
                raise RuntimeError(f"缺少必要环境变量：{name}")
            return value

        room_id_str = require_env("BILI_ROOM_ID")
        tg_chat_id_str = require_env("TG_CHAT_ID")

        bili_room_id = int(room_id_str)
        tg_chat_id = int(tg_chat_id_str)

        allowed_raw = os.getenv("TG_ALLOWED_USER_IDS", "")
        allowed_ids: List[int] = []
        for part in allowed_raw.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                allowed_ids.append(int(part))
            except ValueError:
                # 草，这谁在 TG_ALLOWED_USER_IDS 里塞了奇怪的东西，直接忽略
                continue

        if not allowed_ids:
            # 默认只允许 chat_id 本人操作，避免把直播间控制权交给疯帽子
            allowed_ids.append(tg_chat_id)

        return cls(
            bili_room_id=bili_room_id,
            bili_sessdata=require_env("BILI_SESSDATA"),
            bili_bili_jct=require_env("BILI_BILI_JCT"),
            bili_buvid3=os.getenv("BILI_BUVID3"),
            tg_bot_token=require_env("TG_BOT_TOKEN"),
            tg_chat_id=tg_chat_id,
            tg_allowed_user_ids=allowed_ids,
        )


