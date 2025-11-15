from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional

from bilibili_api import Credential, live


@dataclass
class BiliSender:
    """负责往 B 站直播间里丢弹幕的“小红心女王信使”"""

    room_id: int
    credential: Credential
    _lock: asyncio.Lock

    @classmethod
    def from_cookies(
        cls,
        room_id: int,
        sessdata: str,
        bili_jct: str,
        buvid3: Optional[str] = None,
    ) -> "BiliSender":
        cred = Credential(
            sessdata=sessdata,
            bili_jct=bili_jct,
            buvid3=buvid3,
        )
        return cls(room_id=room_id, credential=cred, _lock=asyncio.Lock())

    async def _send(self, text: str) -> None:
        """真正的发弹幕动作，加一层锁做最小限度的限流序列化。"""

        if not text:
            return

        async with self._lock:
            room = live.LiveRoom(self.room_id, credential=self.credential)
            # 把异常交给上层处理，不在这里吞
            await room.send_danmu(text)

    async def send_plain(self, text: str) -> None:
        """发送普通弹幕。"""

        await self._send(text.strip())

    async def send_reply(self, target_username: str, text: str) -> None:
        """发送带 @ 的“回复”弹幕。

        目前采用简单的 @ 昵称方案，B 站并没有公开稳定的“回复某条弹幕” API。
        """

        text = text.strip()
        if not text:
            return

        prefix = f"@{target_username} "
        await self._send(prefix + text)




