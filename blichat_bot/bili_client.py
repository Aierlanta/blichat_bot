from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable

from blivedm import BLiveClient
from blivedm.models import DanmakuMessage


@dataclass
class DanmakuEvent:
    room_id: int
    uid: int
    uname: str
    text: str


DanmakuCallback = Callable[[DanmakuEvent], Awaitable[None]]


class BiliDanmakuClient(BLiveClient):
    """基于 blivedm 的简单封装，只关心弹幕事件。

    把疯帽子级别复杂的事件流收敛成一个干净的 DanmakuEvent 回调。
    """

    def __init__(self, room_id: int, on_danmaku: DanmakuCallback | None = None) -> None:
        super().__init__(room_id)
        self._on_danmaku: DanmakuCallback | None = on_danmaku
        self._closing = asyncio.Event()

    def set_danmaku_callback(self, cb: DanmakuCallback | None) -> None:
        self._on_danmaku = cb

    async def start_and_run_forever(self) -> None:
        """启动监听并阻塞运行，直到 stop 被调用。

        注意：应在外部事件循环中作为独立任务运行。
        """

        # 连接并开始监听（blivedm 0.1.1 的 start 是同步方法）
        self.start()
        try:
            await self._closing.wait()
        finally:
            await self.close()

    def stop_gracefully(self) -> None:
        """请求停止监听任务。"""

        self._closing.set()

    async def _on_danmaku_msg(  # type: ignore[override]
        self,
        client: "BLiveClient",
        message: DanmakuMessage,
    ) -> None:
        """重写 blivedm 的弹幕回调，只往上抛我们关心的字段。"""

        if not self._on_danmaku:
            return

        event = DanmakuEvent(
            room_id=client.room_id,
            uid=message.uid,
            uname=message.uname,
            text=message.msg,
        )
        await self._on_danmaku(event)


