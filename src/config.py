"""
配置管理模块 - 茶会规则手册
负责加载和验证所有配置项，确保"仙境参数"不出错
"""

from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field, field_validator


class BilibiliConfig(BaseModel):
    """B站直播相关配置"""
    
    room_id: int = Field(..., description="直播间房间号")
    sessdata: str = Field(..., description="SESSDATA Cookie")
    bili_jct: str = Field(..., description="bili_jct Cookie")
    buvid3: str = Field(..., description="buvid3 Cookie")
    
    # Open Live API配置（可选，用于获取完整用户名）
    use_open_live: bool = Field(default=False, description="是否使用Open Live API")
    access_key_id: str = Field(default="", description="Open Live访问密钥ID")
    access_key_secret: str = Field(default="", description="Open Live访问密钥")
    app_id: int = Field(default=0, description="Open Live应用ID")
    auth_code: str = Field(default="", description="房间身份码/认证码")
    
    @field_validator("room_id")
    @classmethod
    def validate_room_id(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("房间号必须大于0，别给我送个负数兔子洞啊喂")
        return v
    
    @field_validator("sessdata", "bili_jct", "buvid3")
    @classmethod
    def validate_cookie(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("Cookie不能为空，没有通行证怎么进仙境？")
        # 检查是否为占位符
        v = v.strip()
        placeholders = ["your_sessdata_here", "your_bili_jct_here", "your_buvid3_here"]
        if v in placeholders:
            raise ValueError(f"Cookie 不能使用占位符 '{v}'，请填写真实的 Cookie 值")
        return v


class TelegramConfig(BaseModel):
    """Telegram Bot相关配置"""
    
    bot_token: str = Field(..., description="Telegram Bot Token")
    chat_id: int = Field(..., description="目标聊天ID（用户ID或频道ID）")
    
    @field_validator("bot_token")
    @classmethod
    def validate_token(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("Bot Token不能为空")
        if ":" not in v:
            raise ValueError("Bot Token格式不对，应该像 '123456:ABC-DEF...' 这样")
        return v.strip()
    
    @field_validator("chat_id")
    @classmethod
    def validate_chat_id(cls, v: int) -> int:
        if v == 0:
            raise ValueError("Chat ID不能为0，得是真实的TG账号ID")
        return v


class BotConfig(BaseModel):
    """机器人行为配置"""
    
    filter_system_message: bool = Field(
        default=True, 
        description="是否过滤系统消息（进场、关注等）"
    )
    danmaku_cooldown: float = Field(
        default=1.0, 
        description="发送弹幕的冷却时间（秒），防止被红心女王砍头"
    )
    message_cache_size: int = Field(
        default=100,
        description="消息映射缓存大小"
    )


class Config(BaseModel):
    """总配置类"""
    
    bilibili: BilibiliConfig
    telegram: TelegramConfig
    bot: BotConfig = Field(default_factory=BotConfig)


def load_config(config_path: Optional[Path] = None) -> Config:
    """
    加载配置文件
    
    Args:
        config_path: 配置文件路径，默认为项目根目录下的 config.yaml
    
    Returns:
        Config实例
    
    Raises:
        FileNotFoundError: 配置文件不存在
        ValueError: 配置格式错误
    """
    if config_path is None:
        config_path = Path("config.yaml")
    
    if not config_path.exists():
        raise FileNotFoundError(
            f"配置文件 {config_path} 不存在！\n"
            f"请复制 config.yaml.example 并填写必要信息"
        )
    
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config_data = yaml.safe_load(f)
        
        return Config(**config_data)
    
    except yaml.YAMLError as e:
        raise ValueError(f"配置文件YAML格式错误：{e}")
    except Exception as e:
        raise ValueError(f"配置加载失败：{e}")


# 全局配置实例（懒加载）
_config: Optional[Config] = None


def get_config() -> Config:
    """获取全局配置实例"""
    global _config
    if _config is None:
        _config = load_config()
    return _config

