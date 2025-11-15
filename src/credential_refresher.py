"""
B站凭证自动刷新器 - Cookie续命魔法
负责检测和刷新B站登录凭证，防止session过期
"""

import asyncio
from typing import Optional
from pathlib import Path

from bilibili_api import Credential
from loguru import logger

from .config import Config, save_config


class CredentialRefresher:
    """
    凭证刷新器
    
    自动检测cookie有效性，必要时刷新并更新配置文件
    """
    
    def __init__(
        self,
        credential: Credential,
        config: Config,
        config_path: Optional[Path] = None,
    ):
        """
        Args:
            credential: Bilibili凭证对象
            config: 配置对象
            config_path: 配置文件路径
        """
        self.credential = credential
        self.config = config
        self.config_path = config_path or Path("config.yaml")
        self._check_task: Optional[asyncio.Task] = None
        self._running = False
    
    async def check_and_refresh_if_needed(self) -> bool:
        """
        检查凭证有效性，如果需要则刷新
        
        Returns:
            是否成功（无需刷新或刷新成功都返回True）
        """
        try:
            # 检查是否有效
            is_valid = await self.credential.check_valid()
            
            if is_valid:
                logger.debug("凭证有效，无需刷新♡")
                return True
            
            logger.warning("⚠️ 凭证已失效！准备刷新...")
            
            # 尝试刷新
            return await self.refresh_credential()
        
        except Exception as e:
            logger.error(f"检查凭证时出错：{e}", exc_info=True)
            return False
    
    async def check_refresh_needed(self) -> bool:
        """
        检查是否需要刷新（即使凭证还有效，但快过期了）
        
        Returns:
            是否需要刷新
        """
        try:
            # bilibili-api提供的check_refresh方法会检查是否需要刷新
            needs_refresh = await self.credential.check_refresh()
            
            if needs_refresh:
                logger.info("🔄 凭证即将过期，建议刷新")
            
            return needs_refresh
        
        except Exception as e:
            logger.error(f"检查刷新需求时出错：{e}", exc_info=True)
            return False
    
    async def refresh_credential(self) -> bool:
        """
        刷新凭证
        
        Returns:
            是否刷新成功
        """
        try:
            logger.info("🔄 开始刷新凭证...")
            
            # 检查是否有ac_time_value
            if not self.credential.ac_time_value:
                logger.error(
                    "❌ 无法刷新：缺少 ac_time_value！\n"
                    "请在config.yaml中添加 ac_time_value，"
                    "从浏览器Console获取：localStorage.getItem('ac_time_value')"
                )
                return False
            
            # 调用bilibili-api的刷新方法
            await self.credential.refresh()
            
            logger.success("✅ 凭证刷新成功！")
            
            # 更新配置并保存
            await self._update_and_save_config()
            
            return True
        
        except Exception as e:
            logger.error(f"❌ 凭证刷新失败：{e}", exc_info=True)
            return False
    
    async def _update_and_save_config(self) -> None:
        """
        更新配置对象并保存到文件
        """
        try:
            # 更新配置中的cookie值
            self.config.bilibili.sessdata = self.credential.sessdata or ""
            self.config.bilibili.bili_jct = self.credential.bili_jct or ""
            
            # 如果有新的ac_time_value，也更新
            if self.credential.ac_time_value:
                self.config.bilibili.ac_time_value = self.credential.ac_time_value
            
            # 保存到文件
            save_config(self.config, self.config_path)
            
            logger.success(f"✅ 配置已更新并保存到 {self.config_path}")
        
        except Exception as e:
            logger.error(f"保存配置失败：{e}", exc_info=True)
            logger.warning("虽然刷新成功了，但配置没保存上。下次重启可能还是会用旧cookie")
    
    async def start_periodic_check(self, interval_hours: float = 24.0) -> None:
        """
        启动定期检查任务
        
        Args:
            interval_hours: 检查间隔（小时）
        """
        if self._running:
            logger.warning("定期检查任务已在运行中")
            return
        
        self._running = True
        self._check_task = asyncio.create_task(
            self._periodic_check_loop(interval_hours)
        )
        
        logger.info(f"✅ 已启动定期凭证检查任务（间隔：{interval_hours}小时）")
    
    async def stop_periodic_check(self) -> None:
        """停止定期检查任务"""
        if not self._running:
            return
        
        self._running = False
        
        if self._check_task and not self._check_task.done():
            self._check_task.cancel()
            try:
                await self._check_task
            except asyncio.CancelledError:
                pass
        
        logger.info("定期凭证检查任务已停止")
    
    async def _periodic_check_loop(self, interval_hours: float) -> None:
        """
        定期检查循环
        """
        interval_seconds = interval_hours * 3600
        
        while self._running:
            try:
                # 等待间隔时间
                await asyncio.sleep(interval_seconds)
                
                if not self._running:
                    break
                
                logger.info("⏰ 执行定期凭证检查...")
                
                # 先检查是否需要刷新
                needs_refresh = await self.check_refresh_needed()
                
                if needs_refresh:
                    # 尝试刷新
                    success = await self.refresh_credential()
                    
                    if success:
                        logger.success("✅ 定期刷新成功")
                    else:
                        logger.error("❌ 定期刷新失败")
                else:
                    # 即使不需要刷新，也检查一下有效性
                    is_valid = await self.credential.check_valid()
                    
                    if is_valid:
                        logger.debug("凭证状态正常♡")
                    else:
                        logger.warning("凭证失效但check_refresh未检测到，尝试强制刷新...")
                        await self.refresh_credential()
            
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"定期检查时出错：{e}", exc_info=True)
                # 出错后继续运行，不中断循环


async def create_refresher_from_config(
    config: Config,
    config_path: Optional[Path] = None,
) -> CredentialRefresher:
    """
    从配置创建刷新器
    
    Args:
        config: 配置对象
        config_path: 配置文件路径
    
    Returns:
        刷新器实例
    """
    # 创建Credential对象
    credential = Credential(
        sessdata=config.bilibili.sessdata,
        bili_jct=config.bilibili.bili_jct,
        buvid3=config.bilibili.buvid3,
        ac_time_value=config.bilibili.ac_time_value or None,
    )
    
    # 创建刷新器
    refresher = CredentialRefresher(
        credential=credential,
        config=config,
        config_path=config_path,
    )
    
    return refresher

