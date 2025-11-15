"""
BiliChat Bot 主程序入口
启动所有服务，连接双向魔法桥
"""

import asyncio
import signal
import sys
from pathlib import Path

from loguru import logger

from src.config import load_config
from src.message_mapper import MessageMapper
from src.bilibili_listener import BilibiliDanmakuListener
from src.bilibili_open_listener import BilibiliOpenLiveListener
from src.bilibili_sender import BilibiliDanmakuSender
from src.telegram_bot import TelegramBot


class BotApplication:
    """
    Bot应用主类
    
    负责：
    1. 初始化所有组件
    2. 启动服务
    3. 优雅关闭
    """
    
    def __init__(self):
        self.config = None
        self.mapper = None
        self.bili_sender = None
        self.tg_bot = None
        self.bili_listener = None
        
        self._shutdown_event = asyncio.Event()
    
    def setup_logger(self) -> None:
        """配置日志"""
        logger.remove()  # 移除默认处理器
        
        # 控制台输出
        logger.add(
            sys.stderr,
            format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
                   "<level>{level: <8}</level> | "
                   "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
                   "<level>{message}</level>",
            level="INFO",
            colorize=True,
        )
        
        # 文件输出
        log_dir = Path("logs")
        log_dir.mkdir(exist_ok=True)
        
        logger.add(
            log_dir / "blichat_{time:YYYY-MM-DD}.log",
            format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} | {message}",
            level="DEBUG",
            rotation="00:00",  # 每天零点轮转
            retention="7 days",  # 保留7天
            encoding="utf-8",
        )
        
        logger.info("日志系统初始化完成")
    
    def setup_signal_handlers(self) -> None:
        """设置信号处理器（优雅关闭）"""
        def signal_handler(sig, frame):
            logger.warning(f"收到信号 {sig}，准备关闭...")
            # 使用 call_soon_threadsafe 在事件循环中安全设置 Event
            # 因为信号处理器在主线程中执行，而 asyncio.Event 需要在事件循环线程中操作
            try:
                loop = asyncio.get_event_loop()
                loop.call_soon_threadsafe(self._shutdown_event.set)
            except RuntimeError:
                # 如果事件循环未运行，直接设置（启动前的信号）
                self._shutdown_event.set()
        
        signal.signal(signal.SIGINT, signal_handler)
        # Windows 兼容：只在支持 SIGTERM 的平台上注册
        if hasattr(signal, 'SIGTERM'):
            signal.signal(signal.SIGTERM, signal_handler)
        
        logger.debug("信号处理器注册完成")
    
    async def initialize(self) -> None:
        """初始化所有组件"""
        logger.info("="*60)
        logger.info("🎭 BiliChat Bot - 双向魔法桥启动中...")
        logger.info("="*60)
        
        # 加载配置
        logger.info("📖 加载配置文件...")
        try:
            self.config = load_config()
            logger.success(f"✅ 配置加载成功，目标房间：{self.config.bilibili.room_id}")
        except Exception as e:
            logger.error(f"❌ 配置加载失败：{e}")
            raise
        
        # 初始化消息映射器
        logger.info("🗺️ 初始化消息映射器...")
        self.mapper = MessageMapper(max_size=self.config.bot.message_cache_size)
        
        # 初始化B站发送器
        logger.info("📤 初始化B站弹幕发送器...")
        self.bili_sender = BilibiliDanmakuSender(
            config=self.config.bilibili,
            cooldown=self.config.bot.danmaku_cooldown,
        )
        
        # 测试B站连接
        logger.info("🔗 测试B站连接...")
        if not await self.bili_sender.test_connection():
            logger.error("❌ B站连接测试失败，请检查Cookie是否正确")
            raise RuntimeError("B站连接失败")
        
        # 初始化TG Bot
        logger.info("🤖 初始化Telegram Bot...")
        self.tg_bot = TelegramBot(
            config=self.config.telegram,
            bili_sender=self.bili_sender,
            message_mapper=self.mapper,
        )
        
        # 初始化B站监听器（根据配置选择）
        if self.config.bilibili.use_open_live:
            logger.info("📡 初始化B站Open Live弹幕监听器（完整用户名模式）...")
            self.bili_listener = BilibiliOpenLiveListener(
                config=self.config.bilibili,
                on_danmaku=self._on_danmaku_received,
                filter_system=self.config.bot.filter_system_message,
            )
        else:
            logger.info("📡 初始化B站Web弹幕监听器（标准模式）...")
            self.bili_listener = BilibiliDanmakuListener(
                config=self.config.bilibili,
                on_danmaku=self._on_danmaku_received,
                filter_system=self.config.bot.filter_system_message,
            )
        
        logger.success("✅ 所有组件初始化完成")
    
    async def _on_danmaku_received(self, user_id: int, uid_crc32: str, username: str, content: str, user_info: dict) -> None:
        """
        弹幕接收回调
        
        Args:
            user_id: B站用户UID
            uid_crc32: 用户身份码
            username: 用户名
            content: 弹幕内容
            user_info: 扩展用户信息
        """
        # 转发到TG
        await self.tg_bot.forward_danmaku(user_id, uid_crc32, username, content, user_info)
    
    async def start(self) -> None:
        """启动所有服务"""
        logger.info("🚀 启动服务...")
        
        # 启动TG Bot
        await self.tg_bot.start()
        
        # 在后台任务中启动B站监听器
        listener_task = asyncio.create_task(self.bili_listener.start())
        
        logger.success("="*60)
        logger.success("✨ BiliChat Bot 启动完成！魔法桥已连接~")
        logger.success("="*60)
        
        # 等待关闭信号
        try:
            await self._shutdown_event.wait()
        except asyncio.CancelledError:
            pass
        
        # 关闭服务
        await self.shutdown(listener_task)
    
    async def shutdown(self, listener_task: asyncio.Task) -> None:
        """优雅关闭所有服务"""
        logger.info("="*60)
        logger.info("🛑 正在关闭所有服务...")
        logger.info("="*60)
        
        # 停止B站监听器
        if self.bili_listener:
            logger.info("📡 停止弹幕监听器...")
            await self.bili_listener.stop()
            
            # 等待监听任务完成
            try:
                await asyncio.wait_for(listener_task, timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("监听器停止超时，强制取消")
                listener_task.cancel()
                # 等待任务清理资源（防止资源泄漏）
                try:
                    await listener_task
                except asyncio.CancelledError:
                    pass
        
        # 停止TG Bot
        if self.tg_bot:
            logger.info("🤖 停止Telegram Bot...")
            await self.tg_bot.stop()
        
        # 清理映射缓存
        if self.mapper:
            self.mapper.clear()
        
        logger.success("="*60)
        logger.success("👋 BiliChat Bot 已安全关闭，下次再见~")
        logger.success("="*60)
    
    async def run(self) -> None:
        """主运行流程"""
        try:
            await self.initialize()
            await self.start()
        except KeyboardInterrupt:
            logger.info("检测到Ctrl+C，正在关闭...")
        except Exception as e:
            logger.exception(f"运行时发生错误：{e}")
            raise
        finally:
            logger.info("程序退出")


async def main():
    """异步主函数"""
    app = BotApplication()
    
    # 设置日志
    app.setup_logger()
    
    # 设置信号处理
    app.setup_signal_handlers()
    
    # 运行
    await app.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n程序已终止")
    except Exception as e:
        logger.exception(f"程序崩溃：{e}")
        sys.exit(1)
