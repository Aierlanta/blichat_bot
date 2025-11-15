"""
B站直播弹幕监听器 - 仙境入口守望者
负责连接B站直播间，实时接收弹幕并分发给处理器
"""

import asyncio
from typing import Callable, Awaitable

import blivedm
from blivedm.models import web
from loguru import logger

from .config import BilibiliConfig


class BilibiliDanmakuListener:
    """
    B站弹幕监听器
    
    基于blivedm库，监听指定直播间的弹幕消息
    过滤掉系统消息（进场、关注等），只保留真实弹幕
    """
    
    def __init__(
        self,
        config: BilibiliConfig,
        on_danmaku: Callable[[int, str, str, str, dict], Awaitable[None]],
        filter_system: bool = True,
    ):
        """
        Args:
            config: B站配置
            on_danmaku: 弹幕回调函数 (user_id, username, content) -> None
            filter_system: 是否过滤系统消息
        """
        self.config = config
        self.on_danmaku = on_danmaku
        self.filter_system = filter_system
        
        # 创建blivedm客户端
        self.client = blivedm.BLiveClient(
            room_id=config.room_id,
            session=None,  # 使用默认session
        )
        
        # 注册处理器
        self.handler = DanmakuHandler(
            on_danmaku=on_danmaku,
            filter_system=filter_system,
        )
        self.client.set_handler(self.handler)
        
        self._running = False
        logger.info(f"弹幕监听器初始化完成，目标房间：{config.room_id}")
    
    async def start(self) -> None:
        """启动监听"""
        if self._running:
            logger.warning("监听器已在运行中，忽略重复启动")
            return
        
        self._running = True
        logger.info(f"开始监听直播间 {self.config.room_id} 的弹幕...")
        
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
        # client.stop() 不返回awaitable，直接调用
        self.client.stop()
        
        # 等待完全停止
        await asyncio.sleep(0.5)
    
    @property
    def is_running(self) -> bool:
        """是否正在运行"""
        return self._running


class DanmakuHandler(blivedm.BaseHandler):
    """
    弹幕处理器
    
    继承自blivedm的BaseHandler，处理各类直播间消息
    """
    
    def __init__(
        self,
        on_danmaku: Callable[[int, str, str], Awaitable[None]],
        filter_system: bool = True,
    ):
        super().__init__()
        self.on_danmaku = on_danmaku
        self.filter_system = filter_system
    
    def _on_danmaku(self, client: blivedm.BLiveClient, message: web.DanmakuMessage):
        """
        处理弹幕消息
        
        消息结构包含丰富的用户信息
        """
        try:
            user_id = message.uid or 0
            uid_crc32 = message.uid_crc32 or ""  # B站的用户身份码
            username = message.uname
            content = message.msg
            
            # 调试：使用uid_crc32作为用户标识
            if user_id == 0:
                logger.debug(
                    f"UID为0，使用uid_crc32标识用户：{uid_crc32[:8]}..."
                )
            
            # 收集扩展用户信息
            user_info = {
                "user_level": message.user_level or 0,
                "medal_name": message.medal_name or "",
                "medal_level": message.medal_level or 0,
                "vip": message.vip or 0,
                "admin": message.admin or False,
                "title": message.title or "",
            }
            
            logger.debug(f"收到弹幕：[{username}({uid_crc32[:8]})] {content}")
            
            # 创建异步任务调用回调，并捕获异常（防止异常被静默吞掉）
            task = asyncio.create_task(self.on_danmaku(user_id, uid_crc32, username, content, user_info))
            
            # 添加异常回调，确保异常被记录
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
    
    def _on_gift(self, client: blivedm.BLiveClient, message: web.GiftMessage):
        """
        处理礼物消息（可选记录，但不转发）
        """
        if not self.filter_system:
            logger.debug(
                f"收到礼物：{message.uname} 赠送了 {message.gift_name} x{message.num}"
            )
    
    def _on_buy_guard(self, client: blivedm.BLiveClient, message: web.GuardBuyMessage):
        """处理上舰消息"""
        if not self.filter_system:
            logger.debug(f"收到上舰：{message.username} 开通了 {message.gift_name}")
    
    def _on_super_chat(self, client: blivedm.BLiveClient, message: web.SuperChatMessage):
        """
        处理醒目留言（SC）
        
        SC通常也算作弹幕的一种，可以选择转发
        """
        try:
            user_id = message.uid or 0
            uid_crc32 = message.uid_crc32 or ""
            username = message.uname
            content = message.message
            
            logger.info(f"收到SC：[{username}({uid_crc32[:8]})] ¥{message.price} - {content}")
            
            # SC也转发（带价格标记）
            sc_content = f"💰¥{message.price} {content}"
            
            # 收集用户信息
            user_info = {
                "user_level": message.user_level or 0,
                "medal_name": message.medal_name or "",
                "medal_level": message.medal_level or 0,
                "vip": message.vip or 0,
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

