"""
B站Open Live API弹幕监听器 - 获取完整用户信息
使用B站Open Live API，可以获取未脱敏的用户名和更多信息
"""

import asyncio
from typing import Callable, Awaitable

import blivedm
from blivedm.models import open_live as open_models
from loguru import logger

from .config import BilibiliConfig


class BilibiliOpenLiveListener:
    """
    B站Open Live API弹幕监听器
    
    使用Open Live API，可以获取：
    - 完整的用户名（未脱敏）
    - 真实的用户UID
    - 更详细的用户信息
    
    需要主播权限和身份码
    """
    
    def __init__(
        self,
        config: BilibiliConfig,
        on_danmaku: Callable[[int, str, str, str, dict], Awaitable[None]],
        filter_system: bool = True,
    ):
        """
        Args:
            config: B站配置（需包含Open Live API配置）
            on_danmaku: 弹幕回调函数
            filter_system: 是否过滤系统消息
        """
        self.config = config
        self.on_danmaku = on_danmaku
        self.filter_system = filter_system
        
        # 创建OpenLiveClient
        self.client = blivedm.OpenLiveClient(
            access_key_id=config.access_key_id,
            access_key_secret=config.access_key_secret,
            app_id=config.app_id,
            room_owner_auth_code=config.auth_code,
        )
        
        # 注册处理器
        self.handler = OpenLiveDanmakuHandler(
            on_danmaku=on_danmaku,
            filter_system=filter_system,
        )
        self.client.set_handler(self.handler)
        
        self._running = False
        logger.info(f"Open Live弹幕监听器初始化完成，目标房间：{config.room_id}")
    
    async def start(self) -> None:
        """启动监听"""
        if self._running:
            logger.warning("监听器已在运行中，忽略重复启动")
            return
        
        self._running = True
        logger.info(f"开始监听直播间 {self.config.room_id} 的弹幕（Open Live API）...")
        
        try:
            # 启动客户端
            self.client.start()
            # 等待客户端结束（会阻塞直到停止）
            await self.client.join()
        except Exception as e:
            logger.error(f"弹幕监听异常：{e}")
            raise
        finally:
            self._running = False
            logger.info("弹幕监听已停止")
    
    async def stop(self) -> None:
        """停止监听"""
        if not self._running:
            logger.warning("监听器未运行，忽略停止请求")
            return
        
        logger.info("正在停止弹幕监听...")
        await self.client.stop_and_close()
        
        # 等待完全停止
        await asyncio.sleep(0.5)
    
    @property
    def is_running(self) -> bool:
        """是否正在运行"""
        return self._running


class OpenLiveDanmakuHandler(blivedm.BaseHandler):
    """
    Open Live API弹幕处理器
    """
    
    def __init__(
        self,
        on_danmaku: Callable[[int, str, str, str, dict], Awaitable[None]],
        filter_system: bool = True,
    ):
        super().__init__()
        self.on_danmaku = on_danmaku
        self.filter_system = filter_system
    
    def _on_open_live_danmaku(self, client: blivedm.OpenLiveClient, message: open_models.DanmakuMessage):
        """
        处理Open Live弹幕消息
        
        优势：获取完整用户信息！
        """
        try:
            user_id = message.uid  # Open Live API返回真实UID！
            uid_crc32 = ""  # Open Live不需要用crc32
            username = message.uname  # 完整的用户名，未脱敏！
            content = message.msg
            
            logger.info(f"收到弹幕：[{username}(UID:{user_id})] {content}")
            
            # 收集扩展用户信息
            user_info = {
                "user_level": 0,  # Open Live API可能不提供
                "medal_name": message.fan_medal_name or "",
                "medal_level": message.fan_medal_level or 0,
                "vip": 0,  # Open Live API可能不提供
                "admin": False,
                "title": "",
            }
            
            # 创建异步任务并捕获异常
            task = asyncio.create_task(self.on_danmaku(user_id, uid_crc32, username, content, user_info))
            
            def _log_task_exception(t: asyncio.Task) -> None:
                exc = t.exception()
                if exc:
                    logger.error(
                        f"弹幕回调异常：{exc}",
                        exc_info=(type(exc), exc, exc.__traceback__)
                    )
            
            task.add_done_callback(_log_task_exception)
        
        except Exception as e:
            logger.error(f"处理弹幕时出错：{e}", exc_info=True)
    
    def _on_open_live_gift(self, client: blivedm.OpenLiveClient, message: open_models.GiftMessage):
        """处理礼物消息"""
        if not self.filter_system:
            logger.debug(
                f"收到礼物：{message.uname} 赠送了 {message.gift_name} x{message.gift_num}"
            )
    
    def _on_open_live_buy_guard(self, client: blivedm.OpenLiveClient, message: open_models.GuardBuyMessage):
        """处理上舰消息"""
        if not self.filter_system:
            logger.debug(f"收到上舰：{message.user_info.uname} 开通了舰长")
    
    def _on_open_live_super_chat(self, client: blivedm.OpenLiveClient, message: open_models.SuperChatMessage):
        """处理醒目留言（SC）"""
        try:
            user_id = message.uid
            uid_crc32 = ""
            username = message.uname
            content = message.message
            
            logger.info(f"收到SC：[{username}(UID:{user_id})] ¥{message.rmb} - {content}")
            
            # SC也转发（带价格标记）
            sc_content = f"💰¥{message.rmb} {content}"
            
            # 收集用户信息
            user_info = {
                "user_level": 0,
                "medal_name": message.fan_medal_name or "",
                "medal_level": message.fan_medal_level or 0,
                "vip": 0,
                "admin": False,
                "title": "",
            }
            
            # 创建异步任务并捕获异常
            task = asyncio.create_task(self.on_danmaku(user_id, uid_crc32, username, sc_content, user_info))
            
            def _log_task_exception(t: asyncio.Task) -> None:
                exc = t.exception()
                if exc:
                    logger.error(
                        f"SC回调异常：{exc}",
                        exc_info=(type(exc), exc, exc.__traceback__)
                    )
            
            task.add_done_callback(_log_task_exception)
        
        except Exception as e:
            logger.error(f"处理SC时出错：{e}", exc_info=True)

