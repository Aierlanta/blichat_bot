"""
B站直播弹幕监听器 - 仙境入口守望者
负责连接B站直播间，实时接收弹幕并分发给处理器
"""

import asyncio
import dataclasses
from typing import Callable, Awaitable, Any

import blivedm
from blivedm.models import web
from loguru import logger

from .config import BilibiliConfig


@dataclasses.dataclass
class InteractWordMessage:
    """
    进入房间、关注主播等互动消息 (JSON version)
    """
    uid: int = 0
    uname: str = ''
    msg_type: int = 0
    timestamp: int = 0
    
    @classmethod
    def from_command(cls, data: dict):
        return cls(
            uid=data.get('uid', 0),
            uname=data.get('uname', ''),
            msg_type=data.get('msg_type', 1),
            timestamp=data.get('timestamp', 0),
        )


@dataclasses.dataclass
class EntryEffectMessage:
    """
    进场特效消息 (舰长等高贵用户进场)
    """
    uid: int = 0
    target_id: int = 0
    copy_writing: str = ''
    
    @classmethod
    def from_command(cls, data: dict):
        return cls(
            uid=data.get('uid', 0),
            target_id=data.get('target_id', 0),
            copy_writing=data.get('copy_writing', ''),
        )


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
        
        # 等待所有待处理的弹幕回调任务完成（避免资源泄漏）
        await self.handler.wait_all_tasks(timeout=3.0)
        
        # 等待客户端完全停止
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
        on_danmaku: Callable[[int, str, str, str, dict], Awaitable[None]],
        filter_system: bool = True,
    ):
        super().__init__()
        self.on_danmaku = on_danmaku
        self.filter_system = filter_system
        # 跟踪所有待处理的异步任务（防止关闭时被强制取消）
        self._pending_tasks: set = set()
        
        # 注册额外的命令处理器
        self._CMD_CALLBACK_DICT = self._CMD_CALLBACK_DICT.copy()
        self._CMD_CALLBACK_DICT['INTERACT_WORD'] = self._interact_word_callback
        self._CMD_CALLBACK_DICT['ENTRY_EFFECT'] = self._entry_effect_callback

    def _interact_word_callback(self, client, command):
        return self._on_interact_word(client, InteractWordMessage.from_command(command['data']))

    def _entry_effect_callback(self, client, command):
        return self._on_entry_effect(client, EntryEffectMessage.from_command(command['data']))
    
    def _create_task(self, coro: Awaitable[None]) -> None:
        """创建一个被跟踪的异步任务，并处理异常"""
        task = asyncio.create_task(coro)
        self._pending_tasks.add(task)
        
        def _log_task_exception(t: asyncio.Task) -> None:
            self._pending_tasks.discard(t)
            try:
                exc = t.exception()
            except asyncio.CancelledError:
                return
            
            if exc:
                logger.error(
                    f"回调异常：{exc}",
                    exc_info=(type(exc), exc, exc.__traceback__)
                )
        
        task.add_done_callback(_log_task_exception)

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
            
            # 创建异步任务调用回调
            self._create_task(self.on_danmaku(user_id, uid_crc32, username, content, user_info))
        
        except Exception as e:
            logger.error(f"处理弹幕时出错：{e}", exc_info=True)
    
    def _on_gift(self, client: blivedm.BLiveClient, message: web.GiftMessage):
        """
        处理礼物消息
        """
        if not self.filter_system:
            logger.debug(
                f"收到礼物：{message.uname} 赠送了 {message.gift_name} x{message.num}"
            )
            
            content = f"[系统消息] 赠送了 {message.gift_name} x{message.num}"
            # 简单的用户信息
            user_info = {
                "user_level": 0,
                "medal_name": message.medal_name or "",
                "medal_level": message.medal_level or 0,
                "vip": 0,
                "admin": False,
                "title": "",
            }
            
            self._create_task(self.on_danmaku(message.uid, "", message.uname, content, user_info))
    
    def _on_buy_guard(self, client: blivedm.BLiveClient, message: web.GuardBuyMessage):
        """处理上舰消息"""
        if not self.filter_system:
            logger.debug(f"收到上舰：{message.username} 开通了 {message.gift_name}")
            
            content = f"[系统消息] 开通了 {message.gift_name}"
            
            user_info = {
                "user_level": 0,
                "medal_name": "",
                "medal_level": 0,
                "vip": 0,
                "admin": False,
                "title": "",
            }
            
            self._create_task(self.on_danmaku(message.uid, "", message.username, content, user_info))
            
    def _on_interact_word_v2(self, _client: blivedm.BLiveClient, message: web.InteractWordV2Message):
        """处理进场/关注消息"""
        if not self.filter_system:
            # msg_type: 1进入, 2关注, 3分享, 4特别关注, 5互粉, 6点赞
            msg_type_str = "进入直播间"
            if message.msg_type == 2:
                msg_type_str = "关注了直播间"
            elif message.msg_type == 3:
                msg_type_str = "分享了直播间"
            elif message.msg_type == 4:
                msg_type_str = "特别关注了直播间"
            elif message.msg_type == 5:
                msg_type_str = "互粉了直播间"
            elif message.msg_type == 6:
                msg_type_str = "点赞了直播间"
                
            logger.debug(f"交互消息：{message.username} {msg_type_str}")
            
            content = f"[系统消息] {msg_type_str}"
            
            user_info = {
                "user_level": 0,
                "medal_name": "",
                "medal_level": 0,
                "vip": 0,
                "admin": False,
                "title": "",
            }
            
            # InteractWordV2Message 使用 username 字段
            self._create_task(self.on_danmaku(message.uid, "", message.username, content, user_info))

    def _on_interact_word(self, _client: blivedm.BLiveClient, message: InteractWordMessage):
        """处理进场/关注消息 (JSON版)"""
        if not self.filter_system:
            # msg_type: 1进入, 2关注, 3分享, 4特别关注, 5互粉, 6点赞
            msg_type_str = "进入直播间"
            if message.msg_type == 2:
                msg_type_str = "关注了直播间"
            elif message.msg_type == 3:
                msg_type_str = "分享了直播间"
            elif message.msg_type == 4:
                msg_type_str = "特别关注了直播间"
            elif message.msg_type == 5:
                msg_type_str = "互粉了直播间"
            elif message.msg_type == 6:
                msg_type_str = "点赞了直播间"
                
            logger.debug(f"交互消息(JSON)：{message.uname} {msg_type_str}")
            
            content = f"[系统消息] {msg_type_str}"
            
            user_info = {
                "user_level": 0,
                "medal_name": "",
                "medal_level": 0,
                "vip": 0,
                "admin": False,
                "title": "",
            }
            
            self._create_task(self.on_danmaku(message.uid, "", message.uname, content, user_info))

    def _on_entry_effect(self, _client: blivedm.BLiveClient, message: EntryEffectMessage):
        """处理进场特效消息"""
        if not self.filter_system:
            # copy_writing 格式如： "欢迎 舰长 User 进入直播间"
            content = f"[系统消息] {message.copy_writing}"
            logger.debug(f"进场特效：{content}")
            
            user_info = {
                "user_level": 0,
                "medal_name": "",
                "medal_level": 0,
                "vip": 0,
                "admin": False,
                "title": "",
            }
            
            # EntryEffectMessage 中可能没有用户名，只有 copy_writing
            # 这里尝试从 copy_writing 提取或者就用 "舰长"
            # uid 是有的
            
            self._create_task(self.on_danmaku(message.uid, "", "舰长/提督", content, user_info))
    
    def _on_super_chat(self, client: blivedm.BLiveClient, message: web.SuperChatMessage):
        """
        处理醒目留言（SC）
        
        SC通常也算作弹幕的一种，可以选择转发
        """
        try:
            user_id = message.uid or 0
            uid_crc32 = getattr(message, 'uid_crc32', "")
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
                "vip": getattr(message, 'vip', 0),
                "admin": False,
                "title": "",
            }
            
            # 创建异步任务并跟踪
            self._create_task(self.on_danmaku(user_id, uid_crc32, username, sc_content, user_info))
        
        except Exception as e:
            logger.error(f"处理SC时出错：{e}", exc_info=True)
    
    async def wait_all_tasks(self, timeout: float = 5.0) -> None:
        """
        等待所有待处理的任务完成
        
        在关闭监听器时调用，确保所有弹幕回调都已完成，避免资源泄漏
        
        Args:
            timeout: 等待超时时间（秒），超时后强制取消剩余任务
        """
        if not self._pending_tasks:
            logger.debug("没有待处理的弹幕任务")
            return
        
        task_count = len(self._pending_tasks)
        logger.info(f"等待 {task_count} 个弹幕处理任务完成...")
        
        try:
            # 等待所有任务完成（return_exceptions=True 避免异常传播）
            await asyncio.wait_for(
                asyncio.gather(*self._pending_tasks, return_exceptions=True),
                timeout=timeout
            )
            logger.success(f"✅ {task_count} 个任务已完成")
        except asyncio.TimeoutError:
            # 超时后强制取消剩余任务
            remaining = len(self._pending_tasks)
            if remaining > 0:
                logger.warning(f"⚠️ 等待超时，强制取消剩余 {remaining} 个任务")
                for task in list(self._pending_tasks):
                    if not task.done():
                        task.cancel()
                # 给任务一点时间清理
                await asyncio.sleep(0.1)

