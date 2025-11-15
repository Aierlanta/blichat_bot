"""
B站弹幕发送器 - 仙境传声筒
负责向B站直播间发送弹幕，支持@用户功能
"""

import asyncio
import time
from typing import Optional
from pathlib import Path

from bilibili_api import Credential, live
from bilibili_api.utils.danmaku import Danmaku
from loguru import logger
from collections import deque

from .config import BilibiliConfig, Config
from .credential_refresher import CredentialRefresher


class BilibiliDanmakuSender:
    """
    B站弹幕发送器
    
    使用bilibili-api-python发送弹幕到直播间
    实现冷却机制，防止频率过快被封禁
    """
    
    def __init__(
        self,
        config: BilibiliConfig,
        cooldown: float = 1.0,
        full_config: Optional[Config] = None,
        config_path: Optional[Path] = None,
        enable_auto_refresh: bool = True,
    ):
        """
        Args:
            config: B站配置
            cooldown: 发送冷却时间（秒）
            full_config: 完整配置对象（用于保存刷新后的凭证）
            config_path: 配置文件路径
            enable_auto_refresh: 是否启用自动刷新
        """
        self.config = config
        self.cooldown = cooldown
        self.enable_auto_refresh = enable_auto_refresh
        
        # 创建凭证
        self.credential = Credential(
            sessdata=config.sessdata,
            bili_jct=config.bili_jct,
            buvid3=config.buvid3,
            ac_time_value=config.ac_time_value or None,
        )
        
        # 创建直播间对象（用于发送弹幕）
        self.room = live.LiveRoom(
            room_display_id=config.room_id,
            credential=self.credential,
        )
        
        # 凭证刷新器
        self.refresher: Optional[CredentialRefresher] = None
        if enable_auto_refresh and full_config:
            self.refresher = CredentialRefresher(
                credential=self.credential,
                config=full_config,
                config_path=config_path,
            )
            logger.info("✅ 凭证自动刷新已启用")
        elif enable_auto_refresh and not full_config:
            logger.warning("⚠️ 未提供完整配置，凭证自动刷新已禁用")
        
        # 冷却控制
        self._last_send_time = 0.0
        self._send_lock = asyncio.Lock()
        # 自身账号信息（用于识别"自己发的弹幕"）
        self.self_uid: Optional[int] = None
        self.self_username: Optional[str] = None
        # 近期发送记录：用于在 Web 监听模式下抑制"回声"
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
                
                # 如果启用了自动刷新，进行校验/刷新后尝试重试（不再依赖错误关键字匹配）
                if self.refresher and self.enable_auto_refresh:
                    logger.info("检测到发送异常，校验/刷新凭证后重试...")
                    
                    should_refresh = False
                    try:
                        # 优先检查是否建议刷新（临近过期）
                        needs_refresh = await self.refresher.check_refresh_needed()
                        if needs_refresh:
                            should_refresh = True
                        else:
                            # 若未建议刷新，则检查是否仍然有效
                            is_valid = await self.credential.check_valid()
                            if not is_valid:
                                should_refresh = True
                    except Exception as check_e:
                        # 检查流程自身失败时，采取保守策略：尝试刷新一次
                        logger.warning(f"检查凭证状态时出错，将尝试刷新：{check_e}")
                        should_refresh = True
                    
                    if should_refresh:
                        refresh_success = await self.refresher.refresh_credential()
                        
                        if refresh_success:
                            # 刷新room对象
                            self.room = live.LiveRoom(
                                room_display_id=self.config.room_id,
                                credential=self.credential,
                            )
                            
                            logger.info("凭证刷新成功，重试发送...")
                            
                            # 重试一次
                            try:
                                danmaku_obj = Danmaku(text=final_content)
                                await self.room.send_danmaku(danmaku_obj)
                                
                                self._last_send_time = time.time()
                                self._recent_sent.append((final_content, self._last_send_time))
                                
                                logger.success(f"✅ 刷新后弹幕发送成功：{final_content}")
                                return True
                            except Exception as retry_e:
                                logger.error(f"刷新后重试仍然失败：{retry_e}")
                
                return False
    
    async def test_connection(self) -> bool:
        """
        测试连接和凭证是否有效
        
        Returns:
            是否连接成功
        """
        try:
            # 如果启用了自动刷新，先检查凭证
            if self.refresher:
                logger.info("🔍 检查凭证有效性...")
                
                # 检查是否需要刷新
                needs_refresh = await self.refresher.check_refresh_needed()
                
                if needs_refresh:
                    logger.info("🔄 凭证即将过期，尝试刷新...")
                    success = await self.refresher.refresh_credential()
                    
                    if success:
                        logger.success("✅ 凭证刷新成功")
                        # 刷新room对象以使用新凭证
                        self.room = live.LiveRoom(
                            room_display_id=self.config.room_id,
                            credential=self.credential,
                        )
                    else:
                        logger.warning("⚠️ 凭证刷新失败，继续使用旧凭证")
            
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
            
            # 启动定期检查（24小时检查一次）
            if self.refresher:
                await self.refresher.start_periodic_check(interval_hours=24.0)
            
            return True
        
        except Exception as e:
            logger.error(f"❌ 连接测试失败：{e}")
            
            # 如果失败了且启用了自动刷新，尝试刷新后重试
            if self.refresher and self.enable_auto_refresh:
                logger.info("尝试刷新凭证后重试...")
                
                refresh_success = await self.refresher.refresh_credential()
                
                if refresh_success:
                    # 刷新room对象
                    self.room = live.LiveRoom(
                        room_display_id=self.config.room_id,
                        credential=self.credential,
                    )
                    
                    # 重试一次
                    try:
                        from bilibili_api import user
                        me = user.get_self_info(credential=self.credential)
                        user_info = await me
                        username = user_info.get("name", "未知")
                        logger.success(f"✅ 刷新后连接成功，当前用户：{username}")
                        
                        # 刷新后重试成功时，同样启动周期性凭证检查
                        if self.refresher:
                            await self.refresher.start_periodic_check(interval_hours=24.0)
                        
                        return True
                    except Exception as retry_e:
                        logger.error(f"刷新后重试仍然失败：{retry_e}")
            
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

