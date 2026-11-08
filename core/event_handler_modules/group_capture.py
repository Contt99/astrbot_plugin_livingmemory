"""
群聊消息捕获模块
负责捕获和存储群聊中的所有消息
"""

import asyncio
from typing import TYPE_CHECKING

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.platform import MessageType

from ..memory_scope import is_event_memory_allowed, is_suspicious_session_id

if TYPE_CHECKING:
    from ..base.config_manager import ConfigManager
    from ..managers.conversation_manager import ConversationManager
    from .message_utils import MessageUtils


class GroupCapture:
    """群聊消息捕获类"""

    def __init__(
        self,
        config_manager: "ConfigManager",
        conversation_manager: "ConversationManager",
        message_utils: "MessageUtils",
    ):
        """
        初始化群聊消息捕获模块

        Args:
            config_manager: 配置管理器
            conversation_manager: 会话管理器
            message_utils: 消息处理工具
        """
        self.config_manager = config_manager
        self.conversation_manager = conversation_manager
        self.message_utils = message_utils

    @staticmethod
    def _has_image_placeholder(text: str) -> bool:
        """内容里是否还有未被描述的图片占位符"""
        return "[图片]" in (text or "")

    async def _backfill_image_description(
        self,
        message_id: int,
        event: AstrMessageEvent,
        original: str,
    ) -> None:
        """图片转文字的结果由上游图片处理链路异步回填到 message_str。

        被动捕获早于图片处理，含图片的消息此刻只能落成 ``[图片]`` 占位。这里在
        后台等描述出现后把正文补写回去，避免历史窗口和长期记忆里只剩占位符。
        等待期间不阻塞消息入库，也不影响同会话内的消息顺序。
        """
        store = getattr(self.conversation_manager, "store", None)
        update = getattr(store, "update_message_content", None)
        if update is None:
            return
        for _ in range(120):
            await asyncio.sleep(0.5)
            latest = (getattr(event, "message_str", "") or "").strip()
            if not latest or "[图片内容" not in latest:
                continue
            if latest == original:
                return
            if await update(message_id, latest[:2000]):
                logger.debug(
                    f"[图片描述回填] message_id={message_id} 长度={len(latest)}"
                )
            return

    async def handle_all_group_messages(self, event: AstrMessageEvent):
        """Capture all group messages for memory storage"""
        # 检查配置
        if not self.config_manager.get(
            "session_manager.enable_full_group_capture", True
        ):
            return

        # 只处理群聊消息
        if event.get_message_type() != MessageType.GROUP_MESSAGE:
            return

        if not is_event_memory_allowed(self.config_manager, event):
            return

        # 群聊中 Bot 自己的消息由 handle_memory_reflection 负责写入，此处跳过
        # 避免 platform echo 导致 assistant 响应被写入两次
        if event.get_sender_id() == event.get_self_id():
            return

        try:
            session_id = event.unified_msg_origin

            # 检测异常session_id
            if is_suspicious_session_id(session_id):
                logger.warning(
                    f"检测到异常的session_id: {session_id}。"
                    f"这可能是平台适配器初始化问题，建议检查平台配置。"
                )

            # 获取消息内容
            content = await self.message_utils.extract_message_content(event)
            dedup_key = await self.message_utils.build_dedup_key(
                event, session_id, content
            )

            # 消息去重
            if dedup_key and await self.message_utils.is_duplicate_message(dedup_key):
                logger.debug(f"[{session_id}] 消息已存在,跳过: dedup_key={dedup_key}")
                return

            # 存储消息到数据库（群聊用户消息，role 固定为 user）
            stored = await self.conversation_manager.add_message_from_event(
                event=event,
                role="user",
                content=content,
            )
            if stored is not None and self._has_image_placeholder(content):
                # 图片转文字结果由上游链路稍后回填到 message_str，先按占位符入库，
                # 后台等描述出现后再补写正文，避免阻塞消息顺序
                asyncio.create_task(
                    self._backfill_image_description(stored.id, event, content)
                )
            if dedup_key:
                await self.message_utils.mark_message_processed(dedup_key)

            # 执行消息数量上限控制
            await self.message_utils.enforce_message_limit(session_id)

            logger.debug(
                f"[{session_id}] 捕获群聊消息: "
                f"sender={event.get_sender_name()}({event.get_sender_id()}), "
                f"content={content[:50]}..."
            )

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"处理群聊全量消息时发生错误: {e}", exc_info=True)
