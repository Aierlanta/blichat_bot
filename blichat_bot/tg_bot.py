from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import Message, ReplyKeyboardRemove

from .config import Config
from .bili_sender import BiliSender
from .bili_client import DanmakuEvent


@dataclass
class DanmakuMeta:
    room_id: int
    uid: int
    uname: str
    text: str


class TgBiliBridgeBot:
    """Telegram ↔ B 站弹幕的桥接 Bot 核心逻辑。"""

    def __init__(self, config: Config, bili_sender: BiliSender) -> None:
        self._config = config
        self._bili_sender = bili_sender
        self._bot = Bot(
            token=config.tg_bot_token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        self._dp = Dispatcher()

        # 记录“TG 消息 ID → 弹幕元数据”，方便后续在回复时知道要 @ 谁
        self._danmaku_index: Dict[int, DanmakuMeta] = {}

        self._register_handlers()

    @property
    def bot(self) -> Bot:
        return self._bot

    @property
    def dispatcher(self) -> Dispatcher:
        return self._dp

    def _register_handlers(self) -> None:
        @self._dp.message(CommandStart())
        async def handle_start(message: Message) -> None:  # type: ignore[unused-ignore]
            if not self._is_user_allowed(message.from_user.id if message.from_user else None):
                return

            await message.answer(
                "这里是 B 站弹幕 ↔ Telegram 的小兔子中继站。\n"
                "• 回复一条弹幕消息 = 在直播间 @ 对方并回复。\n"
                "• 直接发消息 = 在直播间发一条普通弹幕。",
                reply_markup=ReplyKeyboardRemove(),
            )

        @self._dp.message()
        async def handle_any_message(message: Message) -> None:  # type: ignore[unused-ignore]
            # 只接受白名单用户，避免直播间控制权被路人捣乱
            if not self._is_user_allowed(message.from_user.id if message.from_user else None):
                return

            text = (message.text or "").strip()
            if not text:
                return

            # 如果这是对某条弹幕转发消息的“回复”，则在直播间 @ 原发送者
            if message.reply_to_message:
                await self._handle_reply(message)
            else:
                await self._bili_sender.send_plain(text)

    def _is_user_allowed(self, user_id: int | None) -> bool:
        if user_id is None:
            return False
        return user_id in self._config.tg_allowed_user_ids

    async def _handle_reply(self, message: Message) -> None:
        reply_to = message.reply_to_message
        if not reply_to:
            return

        meta = self._danmaku_index.get(reply_to.message_id)
        if not meta:
            # 草，这条回复找不到原始弹幕映射，只好当普通弹幕发
            await self._bili_sender.send_plain(message.text or "")
            return

        await self._bili_sender.send_reply(meta.uname, message.text or "")

    async def send_danmaku_to_tg(self, event: DanmakuEvent) -> None:
        """把一条 B 站弹幕转发到 Telegram，并记录映射。"""

        text = f"[{event.uname}] {event.text}"
        msg = await self._bot.send_message(
            chat_id=self._config.tg_chat_id,
            text=text,
        )
        self._danmaku_index[msg.message_id] = DanmakuMeta(
            room_id=event.room_id,
            uid=event.uid,
            uname=event.uname,
            text=event.text,
        )

    async def run(self) -> None:
        """启动 Telegram Bot 的轮询。

        注意：应在外部事件循环中作为任务调用，不要阻塞整个 loop。
        """

        await self._dp.start_polling(self._bot, allowed_updates=None)



