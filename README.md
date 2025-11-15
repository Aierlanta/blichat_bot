# 🎭 BiliChat Bot - B站直播弹幕 Telegram 机器人

> *"从直播仙境到TG兔子洞的双向魔法传送门"*

## ✨ 功能特性

- 🎯 **实时弹幕推送**：B站直播间弹幕实时转发到Telegram
- 💬 **双向互动**：
  - 回复TG消息 → 在直播间@原弹幕发送者
  - 直接发TG消息 → 在直播间发送弹幕
- 🔇 **智能过滤**：自动过滤进场、关注等系统消息，只保留真实弹幕
- ⚡ **异步架构**：基于asyncio的高性能并发处理

## 📦 技术栈

- `blivedm` - B站直播弹幕监听
- `python-telegram-bot` - Telegram机器人框架
- `bilibili-api-python` - B站API交互
- `pydantic-settings` - 配置管理

## 🚀 快速开始

### 1. 安装依赖

```bash
# 使用uv安装（推荐）
uv sync

# 或使用pip安装
pip install -e .
```

> 注意：`blivedm`库从GitHub直接安装，首次安装可能需要几分钟

### 2. 配置

复制配置模板并填写：

```bash
cp config.yaml.example config.yaml
```

编辑 `config.yaml`，填入必要信息：

```yaml
bilibili:
  room_id: 你的直播间房间号
  sessdata: "你的SESSDATA Cookie"
  bili_jct: "你的bili_jct Cookie"
  buvid3: "你的buvid3 Cookie"

telegram:
  bot_token: "你的TG Bot Token"
  chat_id: 你的TG用户ID或频道ID
```

### 3. 获取必要凭证

#### 获取B站Cookie

1. 登录B站网页版
2. 打开浏览器开发者工具（F12）
3. 进入 Application/存储 → Cookies → <https://bilibili.com>
4. 找到 `SESSDATA`、`bili_jct`、`buvid3` 的值

#### （可选）获取Open Live API凭证 - 获取完整用户名

> **如果你是主播，可以使用Open Live API获取未脱敏的完整用户名！**

1. 访问 [B站开放平台](https://link.bilibili.com/)
2. 创建项目
3. 获取以下信息：
   - `access_key_id`：访问密钥ID
   - `access_key_secret`：访问密钥Secret
   - `app_id`：应用ID
   - `auth_code`：身份码（主播身份验证码）
4. 在 `config.yaml` 中设置 `use_open_live: true` 并填写上述凭证

**对比：**

- 普通模式：用户名显示为 `产***`（脱敏）
- Open Live模式：用户名显示完整，如 `产房`

#### 获取TG Bot Token

1. 在Telegram中找 @BotFather
2. 发送 `/newbot` 创建新机器人
3. 按提示设置名称和用户名
4. 获取Bot Token

#### 获取TG Chat ID

1. 在Telegram中找 @userinfobot
2. 发送任意消息获取你的用户ID

### 4. 运行

```bash
uv run python main.py
```

## 📖 使用说明

启动后：

- Bot会自动监听配置的直播间弹幕并转发到TG
- 在TG中回复弹幕消息，Bot会在直播间发送"@用户名：你的回复"格式的弹幕
- 在TG中直接发送消息，Bot会作为弹幕发送到直播间

> **注意**：B站直播弹幕的@功能与评论区不同，不会触发用户通知，仅作为文本显示

## ⚠️ 注意事项

- 请妥善保管你的Cookie和Token，不要泄露
- B站Cookie可能会过期，届时需要重新获取
- 发送弹幕有频率限制，请勿滥用
- 本项目仅供学习交流使用

## 📜 License

MIT License
