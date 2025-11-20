"""
blive.chat Open Live 弹幕监听器

通过 blive.chat 的 Open Live 网关获取弹幕：
- 先调用 https://api*.blive.chat/api/open_live/start_game 拿到：
  - websocket_info.wss_link: Open Live WebSocket 地址列表
  - websocket_info.auth_body: 认证数据（JSON）
- 再直连 B 站 Open Live WebSocket，解析 LIVE_OPEN_PLATFORM_DM / SUPER_CHAT 等消息

注意：
- 只负责“真实弹幕 + SC”，不处理进场/关注等系统消息（仍由 Web 监听器负责）
"""

from __future__ import annotations

import asyncio
import json
import struct
import time
import zlib
from typing import Awaitable, Callable, List, Optional

import aiohttp
from loguru import logger

from .config import BilibiliConfig


# WebSocket 协议常量（与 B 站直播弹幕协议兼容）
_HEADER_STRUCT = struct.Struct(">IHHII")  # pack_len, header_len, proto_ver, op, seq
_HEADER_LEN = 16

_OP_HEARTBEAT = 2
_OP_HEARTBEAT_REPLY = 3
_OP_SEND_MSG = 5
_OP_AUTH = 7
_OP_AUTH_REPLY = 8

_PROTO_JSON = 0
_PROTO_INT = 1
_PROTO_ZLIB = 2
_PROTO_BROTLI = 3

_HEARTBEAT_INTERVAL = 10.0  # 秒


class BliveChatFatalError(RuntimeError):
    """表示无需重试的致命错误（例如达到并发上限、身份码无效等）。"""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


class BliveChatOpenLiveListener:
    """
    基于 blive.chat Open Live 的弹幕监听器

    - 使用 blive.chat 提供的 open_live/start_game 作为“代理开放平台”
    - WebSocket 直接连 B 站 Open Live 服务器
    - 解析 LIVE_OPEN_PLATFORM_DM / LIVE_OPEN_PLATFORM_SUPER_CHAT
    - 回调签名与现有监听器保持一致：
      on_danmaku(user_id: int, uid_crc32: str, username: str, content: str, user_info: dict)
    """

    def __init__(
        self,
        config: BilibiliConfig,
        on_danmaku: Callable[[int, str, str, str, dict], Awaitable[None]],
        filter_system: bool = True,
    ) -> None:
        """
        Args:
            config: B 站配置（使用其中的 blive.chat 字段）
            on_danmaku: 弹幕回调协程
            filter_system: 是否过滤系统消息（此监听器本身只处理弹幕/SC）
        """
        self.config = config
        self.on_danmaku = on_danmaku
        self.filter_system = filter_system

        # blive.chat API 相关
        self._api_base: str = (
            config.blive_chat_api_base.strip()
            if getattr(config, "blive_chat_api_base", "")
            else ""
        )
        self._room_key: str = getattr(config, "blive_chat_room_key", "").strip()

        # Open Live 会话信息
        self._game_id: Optional[str] = None
        self._room_owner_open_id: Optional[str] = None
        self._ws_urls: List[str] = []
        self._auth_body: str = ""  # 已序列化的 JSON 字符串

        # 运行时状态
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._running: bool = False
        self._heartbeat_task: Optional[asyncio.Task] = None

        # 跟踪弹幕回调任务，方便优雅关闭
        self._pending_tasks: set[asyncio.Task] = set()

        if not self._room_key:
            logger.warning(
                "BliveChatOpenLiveListener 初始化时未提供 room_key，"
                "请在配置中填写 bilibili.blive_chat_room_key，否则监听将无法工作。"
            )

        logger.info("BliveChat Open Live 监听器初始化完成")

    # ----------------------------------------------------------------------
    # 公共接口
    # ----------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._running

    async def start(self) -> None:
        """启动监听主循环（内部包含自动重连逻辑）。"""
        if self._running:
            logger.warning("BliveChat Open Live 监听器已在运行中，忽略重复启动")
            return

        if not self._room_key:
            logger.error("未配置 bilibili.blive_chat_room_key，无法通过 blive.chat 启动 Open Live")
            return

        self._running = True
        self._session = aiohttp.ClientSession()
        logger.info("开始通过 blive.chat 监听 Open Live 弹幕...")

        try:
            while self._running:
                try:
                    await self._start_once()
                except asyncio.CancelledError:
                    raise
                except BliveChatFatalError as e:
                    # 对于 7007 / 7010 这类错误，不再重试，直接退出监听循环
                    logger.error(
                        f"BliveChat Open Live 遇到致命错误，停止重试：code={e.code}, err={e}",
                        exc_info=True,
                    )
                    self._running = False
                    break
                except Exception as e:
                    logger.error(f"BliveChat Open Live 监听异常：{e}", exc_info=True)

                # 若仍处于运行状态，则等待一小段时间后尝试重连
                if self._running:
                    await asyncio.sleep(5.0)
        finally:
            self._running = False
            await self._close_websocket()
            if self._session is not None:
                await self._session.close()
                self._session = None
            logger.info("BliveChat Open Live 监听器已退出")

    async def stop(self) -> None:
        """请求停止监听。"""
        if not self._running:
            logger.warning("BliveChat Open Live 监听器未运行，忽略停止请求")
            return

        logger.info("正在停止 BliveChat Open Live 监听器...")
        self._running = False
        await self._close_websocket()
        await self.wait_all_tasks(timeout=3.0)
        # 尝试关闭 HTTP 会话（兜底，正常情况下由 start() 的 finally 关闭）
        if self._session is not None:
            try:
                await self._session.close()
            except Exception as e:
                logger.warning(f"关闭 BliveChat HTTP 会话时出错：{e}")
            self._session = None
        # 稍微等待一下网络层完全关闭
        await asyncio.sleep(0.2)

    # ----------------------------------------------------------------------
    # 内部主流程
    # ----------------------------------------------------------------------

    async def _start_once(self) -> None:
        """单次会话：start_game -> 连接 WebSocket -> 读消息直到断开。"""
        assert self._session is not None

        # 如果之前已经有一个有效的 game_id，优先尝试结束旧会话，避免达到并发上限
        if self._game_id:
            try:
                await self._end_game_via_blive()
            except Exception as e:
                logger.warning(f"结束上一轮 Open Live 会话失败（忽略继续尝试）：{e}")

        await self._init_api_base()
        await self._start_game_via_blive()

        if not self._ws_urls or not self._auth_body:
            logger.error("start_game 返回的 websocket_info 无效，无法建立连接")
            return

        # 依次尝试可用的 WebSocket 地址
        for idx, ws_url in enumerate(self._ws_urls):
            if not self._running:
                return

            try:
                logger.info(f"尝试连接 Open Live WebSocket（{idx + 1}/{len(self._ws_urls)}）：{ws_url}")
                await self._connect_and_run(ws_url)
                # 正常返回代表显式 stop 或对端关闭，直接退出本轮
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"连接 {ws_url} 时异常：{e}", exc_info=True)

    async def _init_api_base(self) -> None:
        """初始化 blive.chat API 基地址（若用户未在配置中显式指定）。"""
        if self._api_base:
            return

        # 优先尝试 api1 的 /api/endpoints
        default_endpoints = ["https://api1.blive.chat", "https://api2.blive.chat"]
        endpoints: List[str] = []

        try:
            assert self._session is not None
            async with self._session.get(
                "https://api1.blive.chat/api/endpoints", timeout=5
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    endpoints = data.get("endpoints", []) or []
        except Exception as e:
            logger.warning(f"获取 blive.chat endpoints 失败，将使用默认列表：{e}")

        for ep in endpoints or default_endpoints:
            if ep:
                self._api_base = ep.rstrip("/")
                break

        logger.info(f"选择 blive.chat API 基地址：{self._api_base}")

    async def _start_game_via_blive(self) -> None:
        """调用 blive.chat 的 open_live/start_game，获取 Open Live 连接信息。"""
        assert self._session is not None

        url = f"{self._api_base}/api/open_live/start_game"
        payload = {"code": self._room_key, "app_id": 0}
        headers = {
            # 模拟来自浏览器的访问，降低被风控的概率
            "Origin": "https://blive.chat",
            "Referer": f"https://blive.chat/room/{self._room_key}?roomKeyType=2",
            "User-Agent": "BiliChatBot/0.1 (+https://github.com/)",
        }

        logger.info(f"调用 blive.chat start_game：{url}")
        async with self._session.post(url, json=payload, headers=headers, timeout=10) as resp:
            text = await resp.text()
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                raise RuntimeError(f"blive.chat start_game 返回非法 JSON：{text!r}")

        code = data.get("code", -1)
        if code != 0:
            msg = data.get("message", "")
            req_id = data.get("request_id", "")
            # 参考 blive.chat 前端实现，7007/7010 被视为“业务致命错误”，不应无限重试
            if code in (7007, 7010):
                raise BliveChatFatalError(
                    code,
                    (
                        f"blive.chat start_game 失败：code={code}, message={msg}, "
                        f"request_id={req_id}"
                    ),
                )
            raise RuntimeError(
                f"blive.chat start_game 失败：code={code}, message={msg}, request_id={req_id}"
            )

        payload_data = data.get("data") or {}
        game_info = payload_data.get("game_info") or {}
        ws_info = payload_data.get("websocket_info") or {}
        anchor_info = payload_data.get("anchor_info") or {}

        self._game_id = str(game_info.get("game_id") or "")
        self._ws_urls = [u for u in ws_info.get("wss_link", []) if u]
        # 注意：auth_body 是已序列化的 JSON 字符串，不是 dict
        self._auth_body = ws_info.get("auth_body") or ""
        self._room_owner_open_id = anchor_info.get("open_id") or None

        if not self._ws_urls or not self._auth_body:
            raise RuntimeError("blive.chat start_game 返回缺少 websocket_info，无法连接")

        logger.info(
            f"blive.chat Open Live 会话已创建：game_id={self._game_id}, "
            f"ws_count={len(self._ws_urls)}"
        )

    async def _end_game_via_blive(self) -> None:
        """
        调用 blive.chat 的 open_live/end_game 结束当前会话。

        用于：
        - WebSocket 掉线后在下一轮 start_game 前主动清理旧会话
        - 避免 7010 “同一房间启动数量超过上限”
        """
        if not self._game_id:
            return

        assert self._session is not None

        url = f"{self._api_base}/api/open_live/end_game"
        payload = {"app_id": 0, "game_id": self._game_id}

        try:
            async with self._session.post(url, json=payload, timeout=10) as resp:
                text = await resp.text()
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    logger.warning(f"blive.chat end_game 返回非法 JSON：{text!r}")
                    self._game_id = None
                    return

            code = data.get("code", -1)
            # 0=正常，7000/7003 也被官方视作可忽略错误
            if code not in (0, 7000, 7003):
                msg = data.get("message", "")
                req_id = data.get("request_id", "")
                logger.warning(
                    "blive.chat end_game 返回非 0/7000/7003："
                    f"code={code}, message={msg}, request_id={req_id}"
                )
            else:
                logger.info(f"已结束上一轮 Open Live 会话：game_id={self._game_id}, code={code}")
        except Exception as e:
            logger.warning(f"调用 blive.chat end_game 失败：{e}")
        finally:
            # 无论成功与否，本地都不再持有该 game_id，避免重复使用
            self._game_id = None

    async def _connect_and_run(self, ws_url: str) -> None:
        """连接单个 WebSocket 并处理消息，直到被关闭。"""
        assert self._session is not None

        await self._close_websocket()

        logger.info(f"连接 Open Live WebSocket：{ws_url}")
        async with self._session.ws_connect(ws_url, autoping=False) as ws:
            self._ws = ws
            # 发送认证包
            await self._send_auth()
            # 启动心跳
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

            try:
                logger.info("Open Live WebSocket 消息循环开始")
                async for msg in ws:
                    if not self._running:
                        break

                    if msg.type == aiohttp.WSMsgType.BINARY:
                        self._handle_ws_message(msg.data)
                    elif msg.type == aiohttp.WSMsgType.ERROR:
                        err = ws.exception()
                        raise RuntimeError(f"WebSocket 错误：{err}")
                    elif msg.type in (
                        aiohttp.WSMsgType.CLOSE,
                        aiohttp.WSMsgType.CLOSING,
                        aiohttp.WSMsgType.CLOSED,
                    ):
                        logger.info(
                            f"Open Live WebSocket 连接已关闭：type={msg.type}, code={ws.close_code}"
                        )
                        break
            finally:
                # 记录消息循环结束的原因，便于排查频繁重连问题
                try:
                    logger.info(
                        "Open Live WebSocket 消息循环结束：_running={}, ws_closed={}, close_code={}".format(
                            self._running, ws.closed, ws.close_code
                        )
                    )
                except Exception:
                    # 日志不能影响关闭流程
                    pass

                if self._heartbeat_task is not None:
                    self._heartbeat_task.cancel()
                    try:
                        await self._heartbeat_task
                    except asyncio.CancelledError:
                        pass
                    self._heartbeat_task = None

                self._ws = None

    async def _close_websocket(self) -> None:
        """关闭当前 WebSocket 连接（若存在）。"""
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception as e:
                logger.warning(f"关闭 Open Live WebSocket 时出错：{e}")
            finally:
                self._ws = None

    # ----------------------------------------------------------------------
    # WebSocket 编解码
    # ----------------------------------------------------------------------

    async def _send_auth(self) -> None:
        """发送认证数据包。"""
        if self._ws is None or not self._auth_body:
            return

        # auth_body 已经是 JSON 字符串，直接编码即可
        payload = self._auth_body.encode("utf-8")
        packet = self._make_packet(payload, _OP_AUTH, proto_ver=_PROTO_JSON)
        await self._ws.send_bytes(packet)
        logger.debug(f"已发送 Open Live 认证包，长度={len(payload)}")

    async def _heartbeat_loop(self) -> None:
        """周期性发送心跳包。"""
        while self._running and self._ws is not None:
            try:
                packet = self._make_packet(b"{}", _OP_HEARTBEAT, proto_ver=_PROTO_JSON)
                await self._ws.send_bytes(packet)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"发送 Open Live 心跳失败：{e}")
                return

            await asyncio.sleep(_HEARTBEAT_INTERVAL)

    @staticmethod
    def _make_packet(body: bytes, op: int, proto_ver: int = _PROTO_JSON, seq: int = 1) -> bytes:
        """构造 B 站直播协议数据包。"""
        pack_len = _HEADER_LEN + len(body)
        header = _HEADER_STRUCT.pack(pack_len, _HEADER_LEN, proto_ver, op, seq)
        return header + body

    def _handle_ws_message(self, data: bytes) -> None:
        """解析 WebSocket 二进制数据。"""
        offset = 0
        total = len(data)

        while offset + _HEADER_LEN <= total:
            try:
                pack_len, header_len, proto_ver, op, _seq = _HEADER_STRUCT.unpack_from(
                    data, offset
                )
            except struct.error:
                logger.warning("解析 Open Live 数据包头失败，剩余数据长度不足")
                return

            if pack_len <= 0 or offset + pack_len > total:
                logger.warning(
                    f"Open Live 数据包长度异常：pack_len={pack_len}, total={total}, offset={offset}"
                )
                return

            body = data[offset + header_len : offset + pack_len]

            logger.debug(
                f"收到 Open Live 数据包：op={op}, proto_ver={proto_ver}, pack_len={pack_len}"
            )

            if op in (_OP_SEND_MSG, _OP_AUTH_REPLY):
                self._handle_business_message(proto_ver, op, body)
            elif op == _OP_HEARTBEAT_REPLY:
                # 心跳回应，可用于统计在线人数，这里暂时仅做日志
                logger.debug("收到 Open Live 心跳回应")
            else:
                logger.debug(f"收到未知 op={op} 的 Open Live 数据包，忽略")

            offset += pack_len

    def _handle_business_message(self, proto_ver: int, op: int, body: bytes) -> None:
        """根据 proto_ver 解析业务数据。"""
        if proto_ver in (_PROTO_JSON, _PROTO_INT):
            if not body:
                return
            try:
                text = body.decode("utf-8", errors="ignore")
                logger.debug(
                    f"Open Live 业务消息：op={op}, text_snippet={text[:200]!r}"
                )
                self._handle_json_payload(op, text)
            except Exception as e:
                logger.error(f"解析 Open Live JSON 数据失败：{e}")
        elif proto_ver == _PROTO_ZLIB:
            try:
                decompressed = zlib.decompress(body)
            except Exception as e:
                logger.error(f"解压 Open Live zlib 数据失败：{e}")
                return
            self._handle_ws_message(decompressed)
        elif proto_ver == _PROTO_BROTLI:
            try:
                import brotli  # type: ignore
            except Exception as e:  # pragma: no cover - 依赖缺失时日志提示
                logger.error(f"收到 brotli 编码的 Open Live 数据，但未安装 brotli 库：{e}")
                return
            try:
                decompressed = brotli.decompress(body)  # type: ignore[attr-defined]
            except Exception as e:
                logger.error(f"解压 Open Live brotli 数据失败：{e}")
                return
            self._handle_ws_message(decompressed)
        else:
            logger.debug(f"未知的 Open Live proto_ver={proto_ver}，忽略")

    def _handle_json_payload(self, op: int, text: str) -> None:
        """处理已经解码出的 JSON 文本。"""
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            logger.warning(f"Open Live 收到非 JSON 文本：{text!r}")
            return

        if op == _OP_AUTH_REPLY:
            code = payload.get("code", 0)
            if code != 0:
                logger.error(f"Open Live 认证响应错误：{payload}")
            else:
                logger.info("Open Live 认证成功")
            return

        cmd_full = payload.get("cmd", "") or ""
        cmd = cmd_full.split(":", 1)[0]  # 去掉可能的后缀
        data = payload.get("data") or {}

        if cmd == "LIVE_OPEN_PLATFORM_DM":
            self._handle_open_dm(data)
        elif cmd == "LIVE_OPEN_PLATFORM_SUPER_CHAT":
            self._handle_open_super_chat(data)
        else:
            # 其他命令暂时仅做调试日志
            logger.debug(f"忽略 Open Live 命令：{cmd}")

    # ----------------------------------------------------------------------
    # 业务消息 -> 统一弹幕回调
    # ----------------------------------------------------------------------

    def _create_task(self, coro: Awaitable[None]) -> None:
        """创建一个被跟踪的异步任务，并记录异常。"""
        task = asyncio.create_task(coro)
        self._pending_tasks.add(task)

        def _on_done(t: asyncio.Task) -> None:
            self._pending_tasks.discard(t)
            try:
                exc = t.exception()
            except asyncio.CancelledError:
                return
            if exc:
                logger.error("BliveChat Open Live 回调异常：{}", exc, exc_info=True)

        task.add_done_callback(_on_done)

    def _handle_open_dm(self, data: dict) -> None:
        """处理 LIVE_OPEN_PLATFORM_DM（普通弹幕）。"""
        try:
            username = data.get("uname") or ""
            content = data.get("msg") or ""

            # 如果是回复类型，前面补上 @xxx
            reply_uname = data.get("reply_uname") or ""
            if reply_uname:
                content = f"@{reply_uname} {content}"

            open_id = str(data.get("open_id") or "")

            # 粉丝牌信息
            medal_name = ""
            medal_level = 0
            if data.get("fans_medal_wearing_status"):
                medal_name = data.get("fans_medal_name") or ""
                medal_level = int(data.get("fans_medal_level") or 0)

            user_info = {
                "user_level": 0,
                "medal_name": medal_name,
                "medal_level": medal_level,
                "vip": 0,
                "admin": bool(data.get("is_admin", False)),
                "title": "",
            }

            # Open Live 只提供 open_id，我们放到 uid_crc32 字段里统一传递
            user_id = 0
            uid_crc32 = open_id

            logger.debug(f"BliveChat Open Live 弹幕：[{username}] {content}")
            self._create_task(self.on_danmaku(user_id, uid_crc32, username, content, user_info))
        except Exception as e:
            logger.error(f"处理 Open Live DM 消息失败：{e}", exc_info=True)

    def _handle_open_super_chat(self, data: dict) -> None:
        """处理 LIVE_OPEN_PLATFORM_SUPER_CHAT（醒目留言）。"""
        try:
            username = data.get("uname") or ""
            content = data.get("message") or ""
            price = data.get("rmb", data.get("price", 0))

            open_id = str(data.get("open_id") or "")

            medal_name = ""
            medal_level = 0
            if data.get("fans_medal_wearing_status"):
                medal_name = data.get("fans_medal_name") or ""
                medal_level = int(data.get("fans_medal_level") or 0)

            sc_content = f"💰¥{price} {content}"

            user_info = {
                "user_level": 0,
                "medal_name": medal_name,
                "medal_level": medal_level,
                "vip": 0,
                "admin": False,
                "title": "",
            }

            user_id = 0
            uid_crc32 = open_id

            logger.info(f"BliveChat Open Live SC：[{username}] ¥{price} - {content}")
            self._create_task(self.on_danmaku(user_id, uid_crc32, username, sc_content, user_info))
        except Exception as e:
            logger.error(f"处理 Open Live SC 消息失败：{e}", exc_info=True)

    # ----------------------------------------------------------------------
    # 任务收尾
    # ----------------------------------------------------------------------

    async def wait_all_tasks(self, timeout: float = 5.0) -> None:
        """
        等待所有待处理的弹幕回调任务完成。

        在关闭监听器时调用，确保资源不泄漏。
        """
        if not self._pending_tasks:
            logger.debug("BliveChat Open Live 无待处理任务")
            return

        task_count = len(self._pending_tasks)
        logger.info(f"等待 {task_count} 个 BliveChat Open Live 弹幕任务完成...")

        try:
            await asyncio.wait_for(
                asyncio.gather(*self._pending_tasks, return_exceptions=True),
                timeout=timeout,
            )
            logger.success(f"✅ BliveChat Open Live 任务已全部完成 ({task_count} 个)")
        except asyncio.TimeoutError:
            remaining = len(self._pending_tasks)
            if remaining > 0:
                logger.warning(f"⚠️ 等待超时，强制取消剩余 {remaining} 个任务")
                for task in list(self._pending_tasks):
                    if not task.done():
                        task.cancel()
                await asyncio.sleep(0.1)


