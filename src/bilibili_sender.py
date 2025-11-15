"""
B站弹幕发送器 - 仙境传声筒
负责向B站直播间发送弹幕，支持@用户功能
"""

import asyncio
import time
from typing import Optional

from bilibili_api import Credential, live
from bilibili_api.utils.danmaku import Danmaku
from loguru import logger

from .config import BilibiliConfig


class BilibiliDanmakuSender:
    """
    B站弹幕发送器
    
    使用bilibili-api-python发送弹幕到直播间
    实现冷却机制，防止频率过快被封禁
    """
    
    def __init__(self, config: BilibiliConfig, cooldown: float = 1.0):
        """
        Args:
            config: B站配置
            cooldown: 发送冷却时间（秒）
        """
        self.config = config
        self.cooldown = cooldown
        
        # 创建凭证
        self.credential = Credential(
            sessdata=config.sessdata,
            bili_jct=config.bili_jct,
            buvid3=config.buvid3,
        )
        
        # 创建直播间对象（用于发送弹幕）
        self.room = live.LiveRoom(
            room_display_id=config.room_id,
            credential=self.credential,
        )
        
        # 冷却控制
        self._last_send_time = 0.0
        self._send_lock = asyncio.Lock()
        
        logger.info(f"弹幕发送器初始化完成，目标房间：{config.room_id}")
    
    async def send_danmaku(
        self,
        content: str,
        at_uid: Optional[int] = None,
        at_uid_crc32: Optional[str] = None,
        at_username: Optional[str] = None,
    ) -> bool:
        """
        发送弹幕
        
        Args:
            content: 弹幕内容
            at_uid: 要@的用户UID（可选，通常为0）
            at_uid_crc32: 用户身份码（B站隐私保护标识）
            at_username: 要@的用户名（可选，用于日志）
        
        Returns:
            是否发送成功
        """
        async with self._send_lock:
            # 检查冷却时间
            now = time.time()
            elapsed = now - self._last_send_time
            
            if elapsed < self.cooldown:
                wait_time = self.cooldown - elapsed
                logger.debug(f"冷却中，等待 {wait_time:.1f}秒...")
                await asyncio.sleep(wait_time)
            
            # 构造弹幕内容
            if at_uid_crc32:  # 使用uid_crc32判断是否为回复
                # B站直播弹幕的@功能有限，使用明显的文本格式
                # 格式：@用户名：回复内容
                final_content = f"@{at_username}：{content}"
                logger.info(f"准备发送回复弹幕：{final_content} (目标用户: {at_uid_crc32[:8]}...)")
            else:
                final_content = content
                logger.info(f"准备发送弹幕：{final_content}")
            
            try:
                # 发送弹幕（需要Danmaku对象）
                danmaku_obj = Danmaku(text=final_content)
                await self.room.send_danmaku(danmaku_obj)
                
                self._last_send_time = time.time()
                logger.success(f"✅ 弹幕发送成功：{final_content}")
                return True
            
            except Exception as e:
                logger.error(f"❌ 弹幕发送失败：{e}", exc_info=True)
                return False
    
    async def test_connection(self) -> bool:
        """
        测试连接和凭证是否有效
        
        Returns:
            是否连接成功
        """
        try:
            # 尝试获取用户信息来测试凭证
            from bilibili_api import user
            
            # 通过凭证获取当前用户信息
            me = user.get_self_info(credential=self.credential)
            user_info = await me
            
            username = user_info.get("name", "未知")
            logger.info(f"✅ 连接测试成功，当前用户：{username}")
            logger.info(f"✅ 目标直播间：{self.config.room_id}")
            return True
        
        except Exception as e:
            logger.error(f"❌ 连接测试失败：{e}")
            logger.error("请检查Cookie是否正确或是否已过期")
            return False

