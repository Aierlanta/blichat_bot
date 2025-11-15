from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import List

_DOTENV_LOADED = False


def _load_dotenv(path: str = ".env") -> None:
    """从项目根目录加载 .env 到当前进程的环境变量。

    这个文件形式和 blichat_bot/config.py 里的一致，保持两边用法统一。
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
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


@dataclass
class Config:
    """全局配置，从环境变量 / .env 中读取。"""

    bili_room_id: int
    bili_sessdata: str
    bili_bili_jct: str
    bili_buvid3: str | None

    tg_bot_token: str
    tg_chat_id: int
    tg_allowed_user_ids: List[int]

    @classmethod
    def from_env(cls) -> "Config":
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
                continue

        if not allowed_ids:
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


