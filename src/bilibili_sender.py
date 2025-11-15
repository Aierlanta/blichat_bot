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
from collections import deque

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
        # 自身账号信息（用于识别“自己发的弹幕”）
        self.self_uid: Optional[int] = None
        self.self_username: Optional[str] = None
        # 近期发送记录：用于在 Web 监听模式下抑制“回声”
        self._recent_sent = deque(maxlen=50)  # (text, timestamp)
        
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
            elapsed = time.time() - self._last_send_time
            
            if elapsed < self.cooldown:
                wait_time = self.cooldown - elapsed
                logger.debug(f"冷却中，等待 {wait_time:.1f}秒...")
                await asyncio.sleep(wait_time)
            
            # 记录发送开始时间（用于失败重试逻辑）
            send_start_time = time.time()
            
            # 构造弹幕内容
            if at_uid_crc32:  # 使用uid_crc32判断是否为回复
                # B站直播弹幕的@功能有限，使用明显的文本格式
                # 格式：@用户名：回复内容
                final_content = f"@{at_username}：{content}"
                # 安全处理 uid_crc32 切片（防止 None 或空字符串）
                uid_display = at_uid_crc32[:8] if at_uid_crc32 else "Unknown"
                logger.info(f"准备发送回复弹幕：{final_content} (目标用户: {uid_display}...)")
            else:
                final_content = content
                logger.info(f"准备发送弹幕：{final_content}")
            
            try:
                # 发送弹幕（需要Danmaku对象）
                danmaku_obj = Danmaku(text=final_content)
                await self.room.send_danmaku(danmaku_obj)
                
                # ✅ 成功后才更新时间戳，确保从发送完成时刻开始计算冷却
                self._last_send_time = time.time()
                # 记录近期发送内容（用于回声抑制）
                self._recent_sent.append((final_content, self._last_send_time))
                
                logger.success(f"✅ 弹幕发送成功：{final_content}")
                return True
            
            except Exception as e:
                # 发送失败时重置时间戳，允许立即重试
                self._last_send_time = send_start_time - self.cooldown
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
            # 记录自身账号信息（mid 为用户UID）
            try:
                mid_value = user_info.get("mid") or 0
                mid = int(mid_value)
            except (TypeError, ValueError) as exc:
                logger.warning(
                    f"无法解析当前账号UID，raw_mid={mid_value!r}，将降级为基于内容的回声抑制：{exc}"
                )
                mid = 0
            self.self_uid = mid if mid > 0 else None
            self.self_username = username or None
            logger.info(f"✅ 连接测试成功，当前用户：{username}")
            logger.info(f"✅ 目标直播间：{self.config.room_id}")
            return True
        
        except Exception as e:
            logger.error(f"❌ 连接测试失败：{e}")
            logger.error("请检查Cookie是否正确或是否已过期")
            return False

    def is_self_message(self, user_id: int, username: str, content: str, *, window_seconds: float = 5.0) -> bool:
        """
        判断一条弹幕是否来自本Bot自身，避免“发出后又被监听到再转发”的回声。
        优先依据真实 UID（Open Live 模式可用）；仅当无法获得可靠 UID（例如 Web 模式 uid=0）时，
        才在时间窗口内按内容做一次性去重抑制，避免误伤他人的相同文本。
        """
        # 基于UID判断（Open Live 模式可靠）
        if self.self_uid and user_id and user_id == self.self_uid:
            return True
        # 仅当无法依据UID判断时，才基于近期发送内容做回声抑制
        if not user_id or user_id == 0 or not self.self_uid:
            # 基于近期发送内容判断（Web 监听的回声抑制）
            now = time.time()
            for text, ts in list(self._recent_sent):
                if now - ts <= window_seconds and text == content:
                    return True
        return False

