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
        self.bili_listener = None  # 主弹幕监听器（Web / 官方 Open Live / blive.chat Open Live）
        self.web_system_listener = None  # 仅用于系统消息的 Web 监听器（可选）
        
        # 延迟到事件循环运行后创建，避免在无循环上下文中创建 asyncio 对象
        self._shutdown_event = None
        self._loop = None  # 保存运行中的事件循环引用
    
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
        """
        设置信号处理器（优雅关闭）
        
        注意：此方法必须在 run() 内部调用，确保 self._loop 和 self._shutdown_event 已初始化
        """
        def signal_handler(sig, frame):
            logger.warning(f"收到信号 {sig}，准备关闭...")
            # 使用保存的事件循环引用进行线程安全操作
            # 因为信号处理器在主线程中执行，而 asyncio.Event 需要在事件循环线程中操作
            if self._loop and not self._loop.is_closed():
                self._loop.call_soon_threadsafe(self._shutdown_event.set)
            else:
                # 极端情况：循环已关闭（理论上不应该发生）
                logger.error("无法设置关闭事件：事件循环已关闭")
        
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
        
        # 初始化B站发送器（启用自动刷新）
        logger.info("📤 初始化B站弹幕发送器...")
        self.bili_sender = BilibiliDanmakuSender(
            config=self.config.bilibili,
            cooldown=self.config.bot.danmaku_cooldown,
            full_config=self.config,  # 传入完整配置以支持自动刷新
            config_path=Path("config.yaml"),
            enable_auto_refresh=True,  # 启用自动刷新
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
        bili_cfg = self.config.bilibili
        if getattr(bili_cfg, "use_blive_chat", False):
            # 模式三：通过 blive.chat 代理 Open Live：
            # - 普通弹幕 + SC 走 blive.chat Open Live（完整用户名）
            # - 进场/关注等系统消息仍走 Web 监听器，但只转发 [系统消息]
            from src.blivechat_open_listener import BliveChatOpenLiveListener

            logger.info("📡 初始化B站Web弹幕监听器（仅系统消息）...")
            self.web_system_listener = BilibiliDanmakuListener(
                config=bili_cfg,
                on_danmaku=self._on_system_message_from_web,
                filter_system=self.config.bot.filter_system_message,
            )

            logger.info("📡 初始化Blive.chat Open Live弹幕监听器（完整用户名模式）...")
            self.bili_listener = BliveChatOpenLiveListener(
                config=bili_cfg,
                on_danmaku=self._on_danmaku_received,
                filter_system=self.config.bot.filter_system_message,
            )
        elif bili_cfg.use_open_live:
            logger.info("📡 初始化B站Open Live弹幕监听器（完整用户名模式）...")
            self.bili_listener = BilibiliOpenLiveListener(
                config=bili_cfg,
                on_danmaku=self._on_danmaku_received,
                filter_system=self.config.bot.filter_system_message,
            )
        else:
            logger.info("📡 初始化B站Web弹幕监听器（标准模式）...")
            self.bili_listener = BilibiliDanmakuListener(
                config=bili_cfg,
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
        # 过滤自身弹幕（避免“回声”被再次转发到TG）
        if self.bili_sender and self.bili_sender.is_self_message(user_id, username, content):
            logger.debug("忽略自身弹幕回显")
            return
        # 转发到TG
        await self.tg_bot.forward_danmaku(user_id, uid_crc32, username, content, user_info)

    async def _on_system_message_from_web(
        self,
        user_id: int,
        uid_crc32: str,
        username: str,
        content: str,
        user_info: dict,
    ) -> None:
        """
        Web 弹幕监听器专用回调：只转发系统消息（[系统消息] 开头），避免与 Open Live 弹幕重复。
        """
        if not content.startswith("[系统消息]"):
            # 普通弹幕 / SC 由 Open Live 负责，这里直接丢弃
            return
        await self._on_danmaku_received(user_id, uid_crc32, username, content, user_info)
    
    async def start(self) -> asyncio.Task:
        """
        启动所有服务
        
        Returns:
            listener_task: B站监听器任务（用于后续清理）
        """
        logger.info("🚀 启动服务...")
        
        # 启动TG Bot
        await self.tg_bot.start()
        
        # 在后台任务中启动主 B 站监听器
        listener_task = asyncio.create_task(self.bili_listener.start())

        # 如果存在 Web 系统消息监听器，单独启动一个任务
        self._web_system_task: asyncio.Task | None = None
        if self.web_system_listener is not None:
            self._web_system_task = asyncio.create_task(self.web_system_listener.start())
        
        logger.success("="*60)
        logger.success("✨ BiliChat Bot 启动完成！魔法桥已连接~")
        logger.success("="*60)
        
        # 等待关闭信号
        try:
            await self._shutdown_event.wait()
        except asyncio.CancelledError:
            pass
        
        # 返回 listener_task 供清理使用
        return listener_task
    
    async def shutdown(self, listener_task: asyncio.Task) -> None:
        """
        优雅关闭所有服务（已弃用，使用 _cleanup_components 替代）
        
        此方法保留仅为兼容性，实际清理逻辑已移至 _cleanup_components
        """
        await self._cleanup_components(listener_task)
    
    async def _cleanup_components(self, listener_task: asyncio.Task = None) -> None:
        """
        清理所有已初始化的组件
        
        此方法设计为防御性的，即使部分组件未初始化也能安全执行
        适用于正常关闭和异常退出两种场景
        
        Args:
            listener_task: B站监听器任务（可选，可能为 None）
        """
        logger.info("="*60)
        logger.info("🛑 正在关闭所有服务...")
        logger.info("="*60)
        
        # 停止B站监听器（如果已创建）
        if self.bili_listener:
            try:
                logger.info("📡 停止弹幕监听器...")
                await self.bili_listener.stop()
                
                # 等待监听任务完成（如果任务存在）
                if listener_task:
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
            except Exception as e:
                logger.error(f"停止监听器时出错：{e}", exc_info=True)

        # 停止仅用于系统消息的 Web 监听器（如果存在）
        if self.web_system_listener:
            try:
                logger.info("📡 停止Web系统消息监听器...")
                await self.web_system_listener.stop()

                web_task = getattr(self, "_web_system_task", None)
                if web_task is not None:
                    try:
                        await asyncio.wait_for(web_task, timeout=5.0)
                    except asyncio.TimeoutError:
                        logger.warning("Web系统消息监听器停止超时，强制取消")
                        web_task.cancel()
                        try:
                            await web_task
                        except asyncio.CancelledError:
                            pass
            except Exception as e:
                logger.error(f"停止Web系统消息监听器时出错：{e}", exc_info=True)
        
        # 停止TG Bot（如果已创建）
        if self.tg_bot:
            try:
                logger.info("🤖 停止Telegram Bot...")
                await self.tg_bot.stop()
            except Exception as e:
                logger.error(f"停止TG Bot时出错：{e}", exc_info=True)
        
        # 停止凭证刷新器（如果已启用）
        if self.bili_sender and self.bili_sender.refresher:
            try:
                logger.info("⏹️ 停止凭证自动刷新任务...")
                await self.bili_sender.refresher.stop_periodic_check()
            except Exception as e:
                logger.error(f"停止刷新器时出错：{e}", exc_info=True)
        
        # 清理映射缓存（如果已创建）
        if self.mapper:
            try:
                self.mapper.clear()
            except Exception as e:
                logger.error(f"清理映射缓存时出错：{e}", exc_info=True)
        
        logger.success("="*60)
        logger.success("👋 BiliChat Bot 已安全关闭，下次再见~")
        logger.success("="*60)
    
    async def run(self) -> None:
        """主运行流程"""
        # 在协程中安全获取当前运行的事件循环
        self._loop = asyncio.get_running_loop()
        
        # 在循环运行后创建 Event 对象
        self._shutdown_event = asyncio.Event()
        
        # 此时 self._loop 已就绪，安全注册信号处理器
        self.setup_signal_handlers()
        
        listener_task = None  # 提前声明，用于清理
        
        try:
            await self.initialize()
            listener_task = await self.start()  # start() 返回 listener_task
        except KeyboardInterrupt:
            logger.info("检测到Ctrl+C，正在关闭...")
        except Exception as e:
            logger.exception(f"运行时发生错误：{e}")
            raise
        finally:
            # 确保资源清理（即使初始化失败也要清理已创建的组件）
            logger.info("正在清理资源...")
            await self._cleanup_components(listener_task)
            logger.info("程序退出")


async def main():
    """异步主函数"""
    app = BotApplication()
    
    # 设置日志
    app.setup_logger()
    
    # 运行（信号处理器将在 run() 内部注册，确保循环已就绪）
    await app.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n程序已终止")
    except Exception as e:
        logger.exception(f"程序崩溃：{e}")
        sys.exit(1)
