"""
消息映射管理器 - 时间线记录本
维护TG消息与B站弹幕发送者的映射关系，让回复能找到正确的目标
"""

from collections import OrderedDict
from dataclasses import dataclass
from typing import Optional

from loguru import logger


@dataclass
class DanmakuInfo:
    """弹幕信息"""
    
    user_id: int  # B站用户UID（可能为0）
    uid_crc32: str  # 用户身份码（B站隐私保护，用此标识用户）
    username: str  # 用户名
    content: str  # 弹幕内容
    timestamp: float  # 时间戳
    
    # 扩展用户信息
    user_level: int = 0  # 用户等级
    medal_name: str = ""  # 粉丝牌名称
    medal_level: int = 0  # 粉丝牌等级
    vip: int = 0  # VIP状态 (0=非VIP, 1=月费, 2=年费)
    admin: bool = False  # 是否管理员
    title: str = ""  # 头衔


class MessageMapper:
    """
    消息映射管理器
    
    使用LRU缓存策略，避免内存无限增长
    就像白兔的怀表，只记录最近的时间线
    """
    
    def __init__(self, max_size: int = 100):
        """
        Args:
            max_size: 最大缓存数量
        """
        self._map: OrderedDict[int, DanmakuInfo] = OrderedDict()
        self._max_size = max_size
        logger.info(f"消息映射器初始化，缓存容量：{max_size}")
    
    def add_mapping(self, tg_message_id: int, danmaku: DanmakuInfo) -> None:
        """
        添加映射关系
        
        Args:
            tg_message_id: Telegram消息ID
            danmaku: 弹幕信息
        """
        # 如果已存在，先删除旧的（更新顺序）
        if tg_message_id in self._map:
            del self._map[tg_message_id]
        
        # 添加新映射
        self._map[tg_message_id] = danmaku
        
        # LRU淘汰：超出容量时删除最旧的
        while len(self._map) > self._max_size:
            oldest_id = next(iter(self._map))
            removed = self._map.pop(oldest_id)
            logger.debug(
                f"LRU淘汰：TG消息 {oldest_id} -> "
                f"B站用户 {removed.username}({removed.user_id})"
            )
        
        logger.debug(
            f"添加映射：TG消息 {tg_message_id} -> "
            f"B站用户 {danmaku.username}({danmaku.user_id})"
        )
    
    def get_danmaku(self, tg_message_id: int) -> Optional[DanmakuInfo]:
        """
        根据TG消息ID获取对应的弹幕信息
        
        Args:
            tg_message_id: Telegram消息ID
        
        Returns:
            弹幕信息，如果不存在则返回None
        """
        danmaku = self._map.get(tg_message_id)
        
        if danmaku:
            # 访问后移到最后（LRU更新）
            self._map.move_to_end(tg_message_id)
            logger.debug(f"查询映射：TG消息 {tg_message_id} -> 找到用户 {danmaku.username}")
        else:
            logger.debug(f"查询映射：TG消息 {tg_message_id} -> 未找到（可能已过期）")
        
        return danmaku
    
    def clear(self) -> None:
        """清空所有映射"""
        count = len(self._map)
        self._map.clear()
        logger.info(f"清空映射缓存，共移除 {count} 条记录")
    
    def size(self) -> int:
        """获取当前缓存大小"""
        return len(self._map)

