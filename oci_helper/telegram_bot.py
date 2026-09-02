"""
Telegram Bot 集成模块

对应 Java 原项目：com.yohann.ocihelper.telegram.TgBot
功能：
    - 消息发送（通知、告警、验证码）
    - Bot 命令处理（/start、/status 等）
    - 开机播报
"""

import asyncio
from typing import Optional

from loguru import logger

# Bot 实例引用
_bot_instance: Optional["TelegramNotifier"] = None


class TelegramNotifier:
    """
    Telegram 通知器

    对应 Java: TgBot 和 MessageServiceFactory
    负责通过 Telegram Bot API 发送通知消息。

    使用方式：
        notifier = TelegramNotifier(bot_token, chat_id)
        await notifier.send_message("Hello!")
    """

    def __init__(self, bot_token: str, chat_id: str):
        """
        初始化 Telegram 通知器

        参数:
            bot_token: Telegram Bot Token
            chat_id: 目标 Chat ID
        """
        self.bot_token = bot_token
        self.chat_id = chat_id
        self._bot = None
        self._app = None
        self._lock = asyncio.Lock()
        logger.info("Telegram 通知器已初始化")

    async def initialize(self) -> bool:
        """
        初始化 Bot 实例

        延迟导入 python-telegram-bot，避免未安装时启动失败。
        """
        try:
            from telegram import Bot

            self._bot = Bot(token=self.bot_token)
            await self._bot.initialize()
            await self._bot.get_me()
            logger.info("Telegram Bot 初始化成功")
            return True
        except ImportError:
            logger.warning(
                "python-telegram-bot 未安装，Telegram 通知功能不可用。"
                "请运行: pip install python-telegram-bot"
            )
            return False
        except Exception as exc:
            logger.error("Telegram Bot 初始化失败: error_type={}", type(exc).__name__)
            self._bot = None
            return False

    async def send_message(self, text: str) -> bool:
        """
        发送文本消息

        对应 Java: TgBot.sendMessage()

        参数:
            text: 消息文本

        返回:
            True 如果发送成功
        """
        async with self._lock:
            if not self._bot:
                logger.warning("Telegram Bot 未初始化，消息未发送")
                return False

            try:
                await self._bot.send_message(
                    chat_id=self.chat_id,
                    text=text,
                )
                logger.debug("Telegram 消息已发送")
                return True
            except Exception as exc:
                logger.error("Telegram 消息发送失败: error_type={}", type(exc).__name__)
                return False

    async def send_message_sync(self, text: str) -> bool:
        """
        同步方式发送消息（用于非异步上下文）

        对应 Java 中在普通线程中调用 TgBot.sendMessage 的场景。

        参数:
            text: 消息文本

        返回:
            True 如果发送成功
        """
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # 在已有事件循环中创建新任务
                asyncio.create_task(self.send_message(text))
                return True
            else:
                return asyncio.run(self.send_message(text))
        except Exception as exc:
            logger.error("同步发送消息失败: error_type={}", type(exc).__name__)
            return False

    async def shutdown(self) -> None:
        """关闭 Bot 连接"""
        async with self._lock:
            if self._app:
                try:
                    await self._app.stop()
                    await self._app.shutdown()
                except Exception as exc:
                    logger.error("Telegram Bot 关闭失败: error_type={}", type(exc).__name__)
            if self._bot:
                try:
                    await self._bot.shutdown()
                except Exception as exc:
                    logger.error("Telegram Bot 关闭失败: error_type={}", type(exc).__name__)
            self._bot = None


async def init_telegram_bot(bot_token: str, chat_id: str) -> TelegramNotifier | None:
    """
    初始化全局 Telegram Bot 实例

    参数:
        bot_token: Bot Token
        chat_id: Chat ID

    返回:
        TelegramNotifier 实例，初始化失败时返回 None
    """
    global _bot_instance

    if not bot_token or not chat_id:
        logger.info("Telegram Bot 未配置（缺少 Token 或 Chat ID），跳过初始化")
        return None

    notifier = TelegramNotifier(bot_token, chat_id)
    if not await notifier.initialize():
        return None
    previous = _bot_instance
    _bot_instance = notifier
    if previous is not None and previous is not notifier:
        await previous.shutdown()
    return notifier


async def shutdown_telegram_bot() -> None:
    global _bot_instance
    if _bot_instance is not None:
        await _bot_instance.shutdown()
    _bot_instance = None


def get_bot() -> TelegramNotifier | None:
    """
    获取全局 Telegram Bot 实例

    返回:
        当前 Bot 实例，未初始化时返回 None
    """
    return _bot_instance


async def send_notification(text: str) -> bool:
    """
    便捷函数：发送通知消息

    如果 Bot 未初始化，消息会被静默忽略。

    参数:
        text: 消息文本
    """
    bot = get_bot()
    if bot:
        return await bot.send_message(text)
    else:
        logger.debug("Telegram Bot 未初始化，通知消息已忽略")
        return False
