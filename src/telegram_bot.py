"""
Telegram Bot处理器 - TG兔子洞管家
负责处理TG消息、转发弹幕、处理回复和直接消息
"""

import asyncio
import time
from typing import Optional

from loguru import logger
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

from .config import TelegramConfig
from .message_mapper import MessageMapper, DanmakuInfo
from .bilibili_sender import BilibiliDanmakuSender


class TelegramBot:
    """
    Telegram Bot管理器
    
    负责：
    1. 接收B站弹幕并转发到TG
    2. 处理TG回复消息（@弹幕发送者）
    3. 处理TG直接消息（发送到直播间）
    """
    
    def __init__(
        self,
        config: TelegramConfig,
        bili_sender: BilibiliDanmakuSender,
        message_mapper: MessageMapper,
    ):
        """
        Args:
            config: TG配置
            bili_sender: B站弹幕发送器
            message_mapper: 消息映射管理器
        """
        self.config = config
        self.bili_sender = bili_sender
        self.mapper = message_mapper
        
        # 创建TG应用
        self.app = Application.builder().token(config.bot_token).build()
        
        # 注册处理器
        self._register_handlers()
        
        logger.info("Telegram Bot初始化完成")
    
    def _register_handlers(self) -> None:
        """注册消息处理器"""
        # 命令处理
        self.app.add_handler(CommandHandler("start", self._handle_start))
        self.app.add_handler(CommandHandler("help", self._handle_help))
        self.app.add_handler(CommandHandler("status", self._handle_status))
        
        # 普通消息处理（包括回复和直接消息）
        self.app.add_handler(
            MessageHandler(
                filters.TEXT & filters.ChatType.PRIVATE,
                self._handle_message,
            )
        )
        
        logger.debug("消息处理器注册完成")
    
    async def _handle_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """处理 /start 命令"""
        # 防御性检查：避免空值解引用崩溃
        if not update.message or not update.effective_user:
            logger.warning("收到无效的 Update 对象（message 或 effective_user 为 None）")
            return
        
        welcome_text = (
            "🎭 欢迎来到BiliChat Bot！\n\n"
            "我是连接B站直播间和Telegram的魔法桥~\n\n"
            "✨ 功能说明：\n"
            "- 我会自动将直播间弹幕转发给你\n"
            "- 回复弹幕消息 → 在直播间@原发送者\n"
            "- 直接发送消息 → 在直播间发送弹幕\n\n"
            "💡 使用 /help 查看详细帮助\n"
            "📊 使用 /status 查看运行状态"
        )
        await update.message.reply_text(welcome_text)
        logger.info(f"用户 {update.effective_user.id} 启动了bot")
    
    async def _handle_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """处理 /help 命令"""
        if not update.message or not update.effective_user:
            logger.warning("收到无效的 Update 对象")
            return
        
        help_text = (
            "📖 使用指南\n\n"
            "1️⃣ 接收弹幕\n"
            "Bot会自动推送直播间的弹幕消息\n"
            "格式：[用户名] 弹幕内容\n\n"
            "2️⃣ @弹幕发送者\n"
            "回复Bot发来的弹幕消息，输入你的回复内容\n"
            "Bot会在直播间@原发送者并发送你的回复\n\n"
            "3️⃣ 发送弹幕\n"
            "直接给Bot发送消息（不是回复）\n"
            "Bot会将你的消息作为弹幕发送到直播间\n\n"
            "⚠️ 注意事项：\n"
            "- 弹幕有发送冷却时间，请勿刷屏\n"
            "- 消息映射有缓存限制，太久的消息可能无法回复\n"
            "- 请遵守直播间和平台规则"
        )
        await update.message.reply_text(help_text)
        logger.info(f"用户 {update.effective_user.id} 查看了帮助")
    
    async def _handle_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """处理 /status 命令"""
        if not update.message or not update.effective_user:
            logger.warning("收到无效的 Update 对象")
            return
        
        status_text = (
            f"📊 Bot运行状态\n\n"
            f"🔗 监听房间：{self.bili_sender.config.room_id}\n"
            f"💾 消息缓存：{self.mapper.size()} 条\n"
            f"⏰ 时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"✅ 状态：正常运行"
        )
        await update.message.reply_text(status_text)
        logger.info(f"用户 {update.effective_user.id} 查询了状态")
    
    async def _handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """
        处理普通文本消息
        
        判断是回复消息还是直接消息：
        - 回复消息 → @弹幕发送者
        - 直接消息 → 发送弹幕
        """
        # 防御性检查
        if not update.message or not update.effective_user:
            logger.warning("收到无效的 Update 对象")
            return
        
        message = update.message
        user_id = update.effective_user.id
        
        # 权限检查：只处理配置的chat_id
        if user_id != self.config.chat_id and message.chat_id != self.config.chat_id:
            logger.warning(f"拒绝未授权用户 {user_id} 的消息")
            await message.reply_text("❌ 你没有权限使用此Bot")
            return
        
        # 判断是回复还是直接消息
        if message.reply_to_message:
            # 回复消息 → @弹幕发送者
            await self._handle_reply_message(update, context)
        else:
            # 直接消息 → 发送弹幕
            await self._handle_direct_message(update, context)
    
    async def _handle_reply_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """
        处理回复消息（@弹幕发送者）
        """
        message = update.message
        reply_to = message.reply_to_message
        content = message.text
        
        # 查找原始弹幕信息
        danmaku = self.mapper.get_danmaku(reply_to.message_id)
        
        if not danmaku:
            logger.warning(f"未找到消息 {reply_to.message_id} 的弹幕映射")
            await message.reply_text(
                "❌ 无法找到原始弹幕信息\n"
                "可能是消息太久已过期，或不是弹幕消息"
            )
            return
        
        # 安全处理 uid_crc32 切片（防止空字符串或 None）
        uid_display = danmaku.uid_crc32[:8] if danmaku.uid_crc32 else "Unknown"
        logger.info(
            f"处理回复消息：@{danmaku.username}({uid_display}...) - {content}"
        )
        
        # 发送带@的弹幕
        # 注意：使用uid_crc32作为用户标识（B站隐私保护）
        success = await self.bili_sender.send_danmaku(
            content=content,
            at_uid=danmaku.user_id,
            at_uid_crc32=danmaku.uid_crc32,
            at_username=danmaku.username,
        )
        
        if success:
            await message.reply_text(
                f"✅ 已发送到直播间\n"
                f"回复：@{danmaku.username}：{content}\n\n"
                f"💡 提示：B站直播弹幕的@不会触发通知，仅作为文本显示"
            )
        else:
            await message.reply_text(
                "❌ 发送失败，请检查日志或稍后重试"
            )
    
    async def _handle_direct_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """
        处理直接消息（发送弹幕）
        """
        message = update.message
        content = message.text
        
        # 过滤命令
        if content.startswith("/"):
            return
        
        logger.info(f"处理直接消息：{content}")
        
        # 发送弹幕
        success = await self.bili_sender.send_danmaku(content=content)
        
        if success:
            await message.reply_text("✅ 弹幕已发送到直播间")
        else:
            await message.reply_text("❌ 发送失败，请检查日志或稍后重试")
    
    async def forward_danmaku(
        self,
        user_id: int,
        uid_crc32: str,
        username: str,
        content: str,
        user_info: dict = None,
    ) -> Optional[int]:
        """
        转发B站弹幕到TG
        
        Args:
            user_id: B站用户UID（可能为0）
            uid_crc32: 用户身份码（B站隐私保护标识）
            username: 用户名
            content: 弹幕内容
            user_info: 扩展用户信息
        
        Returns:
            TG消息ID，失败则返回None
        """
        try:
            user_info = user_info or {}
            
            # 构建用户标签
            badges = []
            
            # 粉丝牌
            if user_info.get("medal_name"):
                medal = f"[{user_info['medal_name']}{user_info.get('medal_level', 0)}]"
                badges.append(medal)
            
            # VIP状态
            vip_status = user_info.get("vip", 0)
            if vip_status == 1:
                badges.append("🔷月费")
            elif vip_status == 2:
                badges.append("💎年费")
            
            # 管理员
            if user_info.get("admin"):
                badges.append("🛡️管理")
            
            # 头衔
            if user_info.get("title"):
                badges.append(f"「{user_info['title']}」")
            
            # 用户等级
            user_level = user_info.get("user_level", 0)
            if user_level > 0:
                badges.append(f"UL{user_level}")
            
            # 格式化消息
            badge_str = " ".join(badges) if badges else ""
            if badge_str:
                text = f"💬 {badge_str} [{username}]\n{content}"
            else:
                text = f"💬 [{username}]\n{content}"
            
            # 发送到TG
            sent_message = await self.app.bot.send_message(
                chat_id=self.config.chat_id,
                text=text,
            )
            
            # 记录映射（确保在发送成功后立即执行，并捕获异常）
            try:
                danmaku_info = DanmakuInfo(
                    user_id=user_id,
                    uid_crc32=uid_crc32,
                    username=username,
                    content=content,
                    timestamp=time.time(),
                    user_level=user_info.get("user_level", 0),
                    medal_name=user_info.get("medal_name", ""),
                    medal_level=user_info.get("medal_level", 0),
                    vip=user_info.get("vip", 0),
                    admin=user_info.get("admin", False),
                    title=user_info.get("title", ""),
                )
                self.mapper.add_mapping(sent_message.message_id, danmaku_info)
            except Exception as map_err:
                logger.error(f"映射添加失败，消息ID {sent_message.message_id}: {map_err}", exc_info=True)
            
            logger.debug(f"转发弹幕到TG：{text}")
            return sent_message.message_id
        
        except Exception as e:
            logger.error(f"转发弹幕失败：{e}", exc_info=True)
            return None
    
    async def start(self) -> None:
        """启动Bot"""
        logger.info("启动Telegram Bot...")
        
        # 初始化应用
        await self.app.initialize()
        await self.app.start()
        
        # 开始轮询
        await self.app.updater.start_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )
        
        logger.success("✅ Telegram Bot已启动")
    
    async def stop(self) -> None:
        """停止Bot"""
        logger.info("停止Telegram Bot...")
        
        try:
            # 停止轮询
            if self.app.updater:
                await self.app.updater.stop()
            
            # 停止应用
            await self.app.stop()
            await self.app.shutdown()
            
            logger.success("✅ Telegram Bot已停止")
        
        except Exception as e:
            logger.error(f"停止Bot时出错：{e}")

