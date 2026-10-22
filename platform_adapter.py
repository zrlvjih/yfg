"""班级管理插件 v1.5.3 平台适配层。

不同机器人平台的群管理能力差异很大：

- OneBot（NapCat / go-cqhttp / Lagrange 等）：支持禁言、解除禁言、踢人、撤回；
- QQ 官方机器人：不支持直接禁言，只能记录警告、扣积分并提示管理员手动处理；
- Telegram：通过 ``restrict_chat_member`` 禁言/解除禁言；
- 其他平台：统一返回“不支持”，由调用方降级处理。

本模块只依赖标准库，所有平台对象都通过鸭子类型访问，方便在测试环境中
用简单的假对象替换真实机器人。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

# 平台标识
PLATFORM_QQ_OFFICIAL = "qq_official"
PLATFORM_ONEBOT = "onebot"
PLATFORM_TELEGRAM = "telegram"
PLATFORM_UNKNOWN = "unknown"

PLATFORM_LABELS: dict[str, str] = {
    PLATFORM_QQ_OFFICIAL: "QQ 官方机器人",
    PLATFORM_ONEBOT: "OneBot（NapCat / go-cqhttp / Lagrange）",
    PLATFORM_TELEGRAM: "Telegram",
    PLATFORM_UNKNOWN: "未知平台",
}

# OneBot 协议常见实现名
_ONEBOT_TOKENS = (
    "aiocqhttp",
    "onebot",
    "napcat",
    "go-cqhttp",
    "go_cqhttp",
    "gocqhttp",
    "llonebot",
    "lagrange",
    "cqhttp",
)

# Telegram 全权限（用于解除禁言）
_TELEGRAM_ALLOW_ALL = {
    "can_send_messages": True,
    "can_send_media_messages": True,
    "can_send_polls": True,
    "can_send_other_messages": True,
    "can_add_web_page_previews": True,
    "can_change_info": True,
    "can_invite_users": True,
    "can_pin_messages": True,
}

# Telegram 禁止发言（用于禁言）
_TELEGRAM_DENY_ALL = {key: False for key in _TELEGRAM_ALLOW_ALL}


class PlatformAdapter:
    """平台适配器：屏蔽不同平台群管理 API 的差异。"""

    # ------------------------------------------------------------------
    # 平台识别
    # ------------------------------------------------------------------
    @staticmethod
    def detect(event: Any) -> str:
        """识别当前事件所属平台，返回统一平台标识。"""
        candidates: list[str] = []

        for attr in ("get_platform_name", "get_platform_id"):
            getter = getattr(event, attr, None)
            if callable(getter):
                try:
                    value = getter()
                except Exception:
                    value = None
                if value:
                    candidates.append(str(value))

        platform = getattr(event, "platform", None)
        if isinstance(platform, str):
            candidates.append(platform)
        elif platform is not None:
            for attr in ("platform_name", "name"):
                value = getattr(platform, attr, None)
                if isinstance(value, str) and value:
                    candidates.append(value)

        for attr in ("platform_name", "platform_id"):
            value = getattr(event, attr, None)
            if isinstance(value, str) and value:
                candidates.append(value)

        # 最后回退到机器人类名/模块名
        bot = getattr(event, "bot", None)
        if bot is not None:
            candidates.append(type(bot).__name__)
            candidates.append(type(bot).__module__)

        for candidate in candidates:
            platform = PlatformAdapter.normalize(candidate)
            if platform != PLATFORM_UNKNOWN:
                return platform
        return PLATFORM_UNKNOWN

    @staticmethod
    def normalize(raw: str) -> str:
        """把平台原始名称归一化为统一标识。"""
        value = str(raw or "").strip().lower().replace(" ", "_").replace("-", "_")
        if not value:
            return PLATFORM_UNKNOWN

        if "qq_official" in value or "qqofficial" in value:
            return PLATFORM_QQ_OFFICIAL
        if "official" in value and "qq" in value:
            return PLATFORM_QQ_OFFICIAL
        if any(token in value for token in _ONEBOT_TOKENS):
            return PLATFORM_ONEBOT
        if "telegram" in value or value == "tg":
            return PLATFORM_TELEGRAM
        return PLATFORM_UNKNOWN

    @staticmethod
    def label(platform: str) -> str:
        """返回平台的中文显示名。"""
        return PLATFORM_LABELS.get(platform, PLATFORM_LABELS[PLATFORM_UNKNOWN])

    # ------------------------------------------------------------------
    # 能力查询
    # ------------------------------------------------------------------
    @staticmethod
    def supports_mute(platform: str) -> bool:
        """该平台是否支持直接禁言。"""
        return platform in (PLATFORM_ONEBOT, PLATFORM_TELEGRAM)

    @staticmethod
    def supports_unmute(platform: str) -> bool:
        """该平台是否支持解除禁言。"""
        return platform in (PLATFORM_ONEBOT, PLATFORM_TELEGRAM)

    @staticmethod
    def supports_kick(platform: str) -> bool:
        """该平台是否支持踢出成员（当前仅实现 OneBot）。"""
        return platform == PLATFORM_ONEBOT

    @staticmethod
    def supports_recall(platform: str) -> bool:
        """该平台是否支持撤回消息（当前仅实现 OneBot）。"""
        return platform == PLATFORM_ONEBOT

    @staticmethod
    def capabilities(platform: str) -> dict[str, bool]:
        """返回平台能力表，供 /平台信息 展示。"""
        return {
            "mute": PlatformAdapter.supports_mute(platform),
            "unmute": PlatformAdapter.supports_unmute(platform),
            "kick": PlatformAdapter.supports_kick(platform),
            "recall": PlatformAdapter.supports_recall(platform),
            "warning": True,
            "points": True,
        }

    # ------------------------------------------------------------------
    # 禁言 / 解除禁言
    # ------------------------------------------------------------------
    @staticmethod
    async def mute(event: Any, user_id: str, duration_minutes: int) -> tuple[bool, str]:
        """禁言成员，返回 ``(是否成功, 提示消息)``。"""
        platform = PlatformAdapter.detect(event)
        minutes = max(1, int(duration_minutes or 1))

        if platform == PLATFORM_QQ_OFFICIAL:
            return False, "QQ 官方机器人无法直接禁言，请管理员手动处理"
        if platform == PLATFORM_UNKNOWN:
            return False, f"当前平台 {PlatformAdapter.label(platform)} 不支持禁言功能"

        group_id = PlatformAdapter._group_id(event)
        if not group_id:
            return False, "缺少群号，无法禁言"

        if platform == PLATFORM_ONEBOT:
            ok, error = await PlatformAdapter._invoke(
                event,
                action="set_group_ban",
                direct_name="set_group_ban",
                variants=[
                    {
                        "group_id": PlatformAdapter._coerce_id(group_id),
                        "user_id": PlatformAdapter._coerce_id(user_id),
                        "duration": minutes * 60,
                    },
                ],
            )
            if ok:
                return True, f"已禁言 {minutes} 分钟"
            return False, f"禁言失败：{error}"

        # Telegram：until_date 使用 Unix 时间戳，兼容直接接口与 call_action
        until = int(
            (datetime.now(timezone.utc) + timedelta(minutes=minutes)).timestamp(),
        )
        variants = [
            {
                "chat_id": PlatformAdapter._coerce_id(group_id),
                "user_id": PlatformAdapter._coerce_id(user_id),
                "until_date": until,
                "permissions": dict(_TELEGRAM_DENY_ALL),
            },
            {
                "chat_id": PlatformAdapter._coerce_id(group_id),
                "user_id": PlatformAdapter._coerce_id(user_id),
                "until_date": until,
            },
        ]
        ok, error = await PlatformAdapter._invoke(
            event,
            action="restrict_chat_member",
            direct_name="restrict_chat_member",
            variants=variants,
        )
        if ok:
            return True, f"已禁言 {minutes} 分钟"
        return False, f"禁言失败：{error}"

    @staticmethod
    async def unmute(event: Any, user_id: str) -> tuple[bool, str]:
        """解除成员禁言，返回 ``(是否成功, 提示消息)``。"""
        platform = PlatformAdapter.detect(event)
        if platform == PLATFORM_QQ_OFFICIAL:
            return False, "QQ 官方机器人无法直接解除禁言，请管理员手动处理"
        if platform == PLATFORM_UNKNOWN:
            return False, f"当前平台 {PlatformAdapter.label(platform)} 不支持解除禁言"

        group_id = PlatformAdapter._group_id(event)
        if not group_id:
            return False, "缺少群号，无法解除禁言"

        if platform == PLATFORM_ONEBOT:
            ok, error = await PlatformAdapter._invoke(
                event,
                action="set_group_ban",
                direct_name="set_group_ban",
                variants=[
                    {
                        "group_id": PlatformAdapter._coerce_id(group_id),
                        "user_id": PlatformAdapter._coerce_id(user_id),
                        "duration": 0,
                    },
                ],
            )
            if ok:
                return True, "已解除禁言"
            return False, f"解除禁言失败：{error}"

        # Telegram：until_date=0 表示解除限制
        variants = [
            {
                "chat_id": PlatformAdapter._coerce_id(group_id),
                "user_id": PlatformAdapter._coerce_id(user_id),
                "until_date": 0,
                "permissions": dict(_TELEGRAM_ALLOW_ALL),
            },
            {
                "chat_id": PlatformAdapter._coerce_id(group_id),
                "user_id": PlatformAdapter._coerce_id(user_id),
                "until_date": 0,
            },
        ]
        ok, error = await PlatformAdapter._invoke(
            event,
            action="restrict_chat_member",
            direct_name="restrict_chat_member",
            variants=variants,
        )
        if ok:
            return True, "已解除禁言"
        return False, f"解除禁言失败：{error}"

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    @staticmethod
    def _group_id(event: Any) -> str:
        """读取群 ID，兼容 get_group_id 与属性访问。"""
        getter = getattr(event, "get_group_id", None)
        if callable(getter):
            try:
                value = getter()
            except Exception:
                value = None
            if value:
                return str(value).strip()
        for attr in ("group_id", "chat_id"):
            value = getattr(event, attr, None)
            if value:
                return str(value).strip()
        return ""

    @staticmethod
    def _coerce_id(value: Any) -> Any:
        """数字 ID 转为 int，QQ 官方 openid 等非数字 ID 保持字符串。

        Telegram 超级群 chat_id 可能为负数，因此允许前导 ``-``。
        """
        text = str(value or "").strip()
        digits = text[1:] if text.startswith("-") else text
        return int(text) if digits.isdigit() else text

    @staticmethod
    async def _invoke(
        event: Any,
        action: str,
        direct_name: str,
        variants: list[dict[str, Any]],
    ) -> tuple[bool, str]:
        """依次尝试机器人直接方法与统一的 ``call_action`` 接口。

        ``variants`` 用于兼容不同平台封装版本的参数差异（例如 Telegram
        ``permissions`` 字段）。某一组参数失败时继续尝试下一组，最终返回
        最后一次错误，避免因某个可选参数不被支持而直接放弃。
        """
        bot = getattr(event, "bot", None)
        if bot is None:
            return False, "当前平台未提供机器人实例"

        last_error = ""
        method = getattr(bot, direct_name, None)
        if callable(method):
            for params in variants:
                try:
                    await method(**params)
                    return True, ""
                except Exception as exc:
                    last_error = str(exc) or "平台接口调用失败"

        call_action = getattr(bot, "call_action", None)
        if callable(call_action):
            for params in variants:
                try:
                    await call_action(action, **params)
                    return True, ""
                except Exception as exc:
                    last_error = str(exc) or "平台接口调用失败"

        return False, last_error or "当前平台不支持该操作"
