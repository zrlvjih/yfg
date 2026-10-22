"""AstrBot 班级管理插件 v1.5.4（语法兼容修复版）。

功能：
- 智能交互：免 @ 触发（指令/精确关键词/模糊关键词）、二级菜单、
  智能帮助、30 秒上下文记忆、防刷屏冷却、配置热更新
- v1.5.1：插件消息阻断 LLM、聊天模式（/聊天、/ai、#）、
  本地意图识别与纠错、身份显示（群主/管理员/普通成员）
- v1.5.3：平台适配层（OneBot / QQ 官方 / Telegram）、禁言能力探测、
  不支持禁言时自动降级为警告 + 扣积分、平台信息与惩罚记录指令、
  修复 event.chain_result 传入 MessageChain 导致的 TypeError
- v1.5.4：修复 @ 目标被误判（兼容 openid / 文本 @昵称 / 数字 ID），
  删除商品、奖品、任务与兑换支持“编号或名称”，
  修复数字 ID 被误当作时长或数量的问题，菜单文案与实际功能保持一致
- 每日签到、连续签到奖励
- 积分查询、班级排行榜
- 群聊聊天积分（每日上限、频率限制）
- 经验值、等级与称号系统（升级自动祝贺）
- 积分商城（商品管理、兑换、兑换记录）
- 抽奖系统（奖池管理、10 次保底、中奖记录与统计）
- 每日任务（签到、发言、连续签到，自动检测并发放奖励）
- 成就系统（达成条件自动检测并祝贺）
- 积分转账（手续费、每日限额、新成员限制）
- 积分明细查询
- 群管功能：违禁词（Aho-Corasick 多模式匹配）、防刷屏、防广告、夜间模式
- 成员管理：禁言、解除禁言、踢出、警告、成员信息、违规记录
- 群公告与定时公告、群数据统计、活跃榜/违规榜、白名单
- 群主/管理员积分与商城、抽奖、任务管理
- 每天早上 8 点签到提醒（按群开关）
"""

from __future__ import annotations

import asyncio
import math
import re
import time
import traceback
from collections import deque
from collections.abc import AsyncGenerator, Callable
from contextlib import suppress
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import astrbot.api.message_components as Comp
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star

try:  # AstrBot 环境自带 PyYAML；缺失时退回内置简易解析
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

from .platform_adapter import PlatformAdapter

from .database import (
    AUTO_MOD_LABELS,
    BANNED_PUNISH_ALIASES,
    CHAT_DAILY_LIMIT,
    CHAT_EXP_DAILY_LIMIT,
    CHAT_EXP_PER_MSG,
    CHAT_POINT_PER_MSG,
    DEFAULT_AD_MUTE_MINUTES,
    DEFAULT_BANNED_DURATION,
    DEFAULT_BANNED_PUNISH,
    DEFAULT_NIGHT_MUTE_MINUTES,
    DEFAULT_SPAM_MUTE_MINUTES,
    DEFAULT_SPAM_THRESHOLD,
    DEFAULT_SPAM_WINDOW,
    LOTTERY_COST,
    LOTTERY_PITY,
    MAX_LOTTERY_PRIZES,
    MAX_MUTE_MINUTES,
    SIGN_EXP_REWARD,
    TASK_TYPE_LABELS,
    TRANSFER_DAILY_LIMIT,
    TRANSFER_FEE_RATE,
    TRANSFER_MIN_AGE_DAYS,
    TRANSFER_MIN_FEE,
    ClassDatabase,
    level_progress,
)

# 插件目录与数据库路径
PLUGIN_DIR = Path(__file__).resolve().parent
DATA_DIR = PLUGIN_DIR / "data"
DB_PATH = DATA_DIR / "class_manager.db"

# 不带唤醒前缀时也可以触发的指令
BARE_COMMANDS = {
    "签到",
    "打卡",
    "我的积分",
    "积分排行",
    "排行榜",
    "我的信息",
    "商城",
    "兑换记录",
    "抽奖",
    "奖池",
    "抽奖记录",
    "任务",
    "任务列表",
    "成就",
    "成就列表",
    "转账记录",
    "积分明细",
    "违禁词列表",
    "公告列表",
    "群数据",
    "活跃榜",
    "违规榜",
    "白名单列表",
    "平台信息",
    "惩罚记录",
    "手动惩罚",
    "积分惩罚",
    "帮助",
    "菜单",
}

# 用于注册“无前缀指令”的正则
BARE_COMMAND_PATTERN = (
    r"^(?:签到|打卡|我的积分|积分排行|排行榜|我的信息|商城|兑换记录|"
    r"抽奖|奖池|抽奖记录|任务|任务列表|成就|成就列表|转账记录|积分明细|"
    r"违禁词列表|公告列表|群数据|活跃榜|违规榜|白名单列表|"
    r"平台信息|惩罚记录|手动惩罚|积分惩罚|帮助|菜单)$"
)

# 聊天积分统计时需要跳过的指令词
CHAT_SKIP_WORDS = BARE_COMMANDS | {"积分", "查询积分", "排行", "个人信息"}

# 提醒时间（服务器本地时间）
REMINDER_HOUR = 8
REMINDER_WINDOW_MINUTES = 5

# 自动审核冷却时间（秒）：同一成员短时间内不重复处罚
AUTO_PUNISH_COOLDOWN = 60
NIGHT_PUNISH_COOLDOWN = 600

# 广告检测：链接、域名、@全体成员
AD_LINK_PATTERN = re.compile(
    r"(?:https?://|www\.)"
    r"|(?:[a-zA-Z0-9-]+\.(?:com|cn|net|org|top|xyz|io|cc|vip|live|me|tv|link)\b)"
    r"|(?:t\.me/|qm\.qq\.com/|qun\.qq\.com/)",
    re.IGNORECASE,
)
AD_ALL_PATTERN = re.compile(r"@\s*全体成员|@\s*all\b", re.IGNORECASE)

# 夜间模式时间段：[0 点, 6 点)
NIGHT_START_HOUR = 0
NIGHT_END_HOUR = 6

# ----------------------------------------------------------------------
# 智能交互与惩罚配置定义（v1.5.1 起，v1.5.3 扩展 punishment）
# ----------------------------------------------------------------------
CONFIG_PATH = PLUGIN_DIR / "config.yaml"

# config.yaml 缺失或字段非法时使用的默认配置
DEFAULT_PLUGIN_CONFIG: dict[str, Any] = {
    "trigger_mode": "all",  # all / command / keyword
    "menu_style": "category",  # category / flat
    "cooldown_seconds": 3,
    "context_timeout": 30,
    "session_timeout": 30,  # v1.5.1：会话/上下文统一超时
    "admin_bypass": True,
    # v1.5.1 新增
    "block_llm": True,  # 插件处理的 messages 不再传给 LLM
    "smart_menu": True,  # 启用意图识别、纠错与智能推荐
    "identity_display": True,  # 显示群主/管理员/普通成员身份
    "auto_detect": True,  # 未匹配消息中的问句/请求交给 LLM
    "chat_prefixes": ["/聊天", "/ai", "#"],  # 聊天模式前缀
    "chat_timeout": 30,  # 聊天模式超时（秒）
    # v1.5.3 惩罚配置（平台不支持禁言时自动降级）
    "punishment": {
        "auto_mute": True,  # 自动审核命中时尝试禁言
        "mute_duration": DEFAULT_BANNED_DURATION,  # 手动 /禁言 默认时长（分钟）
        "warning_threshold": 3,  # 累计警告达到该次数后提醒管理员
        "points_deduction": 5,  # 降级惩罚时扣除的积分
        "fallback_to_warning": True,  # 平台不支持禁言时降级为警告 + 扣分
    },
}

# 触发方式的中文别名与显示名
TRIGGER_MODE_ALIASES: dict[str, str] = {
    "全部": "all",
    "所有": "all",
    "all": "all",
    "指令": "command",
    "仅指令": "command",
    "command": "command",
    "关键词": "keyword",
    "仅关键词": "keyword",
    "keyword": "keyword",
    "默认": "",
    "恢复默认": "",
}

TRIGGER_MODE_LABELS: dict[str, str] = {
    "all": "全部（指令 + 关键词）",
    "command": "仅指令（/、！开头）",
    "keyword": "仅关键词（无需前缀）",
}

# 身份角色：中文名 + 图标
IDENTITY_LABELS: dict[str, tuple[str, str]] = {
    "owner": ("👑", "群主"),
    "admin": ("🔧", "管理员"),
    "member": ("👤", "普通成员"),
}

# 聊天模式退出词
CHAT_EXIT_WORDS = {"退出", "退出聊天", "结束聊天", "结束", "再见", "拜拜"}
# 从上下文返回主菜单的词
RETURN_WORDS = {"返回", "返回主菜单", "主菜单", "菜单", "回主页", "退出"}
# 确认/取消词
CONFIRM_YES = {"是", "好", "好的", "确认", "嗯", "对", "是的", "要", "可以"}
CONFIRM_NO = {"否", "不", "不是", "取消", "不要", "算了"}

# 单字缩写 -> 动作键（用于“你是想签到吗？”确认）
SHORT_COMMAND_HINTS: dict[str, str] = {
    "签": "sign_in",
    "到": "sign_in",
    "积": "points",
    "分": "points",
    "商": "shop",
    "抽": "lottery",
    "榜": "leaderboard",
}

# 自然语言意图规则：命中任一短语即视为对应功能
SMART_INTENT_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("多少分", "几分", "分数", "查分", "我的分"), "points"),
    (("能换什么", "可以换", "换什么", "好东西", "兑换什么", "买什么"), "shop"),
    (("签个到", "打个卡", "报道"), "sign_in"),
    (("排第几", "第几名", "排名第"), "leaderboard"),
    (("试试运气", "抽个奖", "抽一次"), "lottery"),
    (("我是谁", "我的等级", "我的资料"), "my_info"),
    (("今天任务", "今日任务"), "tasks"),
    (("什么平台", "平台支持", "能不能禁言", "可以禁言吗"), "platform_info"),
    (("怎么用", "会什么", "能做什么", "有什么功能"), "help"),
)

# 自动识别：疑问句/请求句判定词
QUESTION_WORDS = (
    "什么",
    "怎么",
    "为什么",
    "如何",
    "哪里",
    "哪个",
    "谁",
    "多少",
    "是否",
    "能不能",
    "可不可以",
    "吗",
    "呢",
    "？",
    "?",
)
REQUEST_WORDS = (
    "帮我",
    "帮忙",
    "请",
    "麻烦",
    "写",
    "解释",
    "介绍",
    "翻译",
    "推荐",
    "教我",
    "告诉我",
    "讲一下",
    "说说",
    "总结",
    "生成",
    "分析",
    "计算",
)
GREETING_WORDS = (
    "你好",
    "您好",
    "嗨",
    "hi",
    "hello",
    "早安",
    "早上好",
    "晚上好",
    "晚安",
    "在吗",
)

# 智能触发关键词 -> 动作键。键越长优先级越高（Trie 取最长匹配）。
SMART_KEYWORDS: dict[str, str] = {
    "签到": "sign_in",
    "打卡": "sign_in",
    "签到一下": "sign_in",
    "积分": "points",
    "我的积分": "points",
    "查询积分": "points",
    "查积分": "points",
    "查一下积分": "points",
    "排行榜": "leaderboard",
    "积分排行": "leaderboard",
    "积分榜": "leaderboard",
    "排行": "leaderboard",
    "我的信息": "my_info",
    "个人信息": "my_info",
    "我的资料": "my_info",
    "等级": "my_info",
    "商城": "shop",
    "积分商城": "shop",
    "商店": "shop",
    "兑换记录": "exchange_logs",
    "抽奖": "lottery",
    "抽一次": "lottery",
    "我要抽奖": "lottery",
    "奖池": "lottery_prizes",
    "奖品": "lottery_prizes",
    "抽奖记录": "lottery_logs",
    "任务": "tasks",
    "每日任务": "tasks",
    "今日任务": "tasks",
    "任务列表": "task_list",
    "成就": "achievements",
    "我的成就": "achievements",
    "成就列表": "achievement_list",
    "转账记录": "transfer_logs",
    "积分明细": "points_logs",
    "积分记录": "points_logs",
    "违禁词列表": "banned_list",
    "公告列表": "announcement_list",
    "群数据": "group_stats",
    "群统计": "group_stats",
    "活跃榜": "active_rank",
    "违规榜": "violation_rank",
    "白名单列表": "whitelist_list",
    "平台信息": "platform_info",
    "当前平台": "platform_info",
    "惩罚记录": "punish_logs",
    "手动惩罚": "manual_punish",
    "积分惩罚": "points_punish",
    "帮助": "help",
    "菜单": "help",
    "功能": "help",
    "指令": "help",
}

# 二级菜单：主菜单只展示 6 个分类入口
SMART_CATEGORIES: dict[str, dict[str, Any]] = {
    "1": {
        "key": "points",
        "name": "签到积分",
        "desc": "签到、积分、明细、排行榜",
        "emoji": "1️⃣",
        "entries": [
            {"title": "签到", "action": "sign_in"},
            {"title": "查询积分", "action": "points"},
            {"title": "排行榜", "action": "leaderboard"},
            {"title": "积分明细", "action": "points_logs"},
        ],
    },
    "2": {
        "key": "shop",
        "name": "商城兑换",
        "desc": "商城、兑换、记录、管理",
        "emoji": "2️⃣",
        "entries": [
            {"title": "查看商城", "action": "shop"},
            {"title": "兑换商品", "action": "", "usage": "/兑换 商品编号或名称"},
            {"title": "兑换记录", "action": "exchange_logs"},
            {"title": "查看奖池", "action": "lottery_prizes"},
            {"title": "抽奖一次", "action": "lottery"},
            {"title": "抽奖记录", "action": "lottery_logs"},
        ],
    },
    "3": {
        "key": "level",
        "name": "等级成就",
        "desc": "信息、成就、任务、抽奖",
        "emoji": "3️⃣",
        "entries": [
            {"title": "我的等级", "action": "my_info"},
            {"title": "我的成就", "action": "achievements"},
            {"title": "全部成就", "action": "achievement_list"},
            {"title": "今日任务", "action": "tasks"},
            {"title": "任务列表", "action": "task_list"},
            {"title": "抽奖一次", "action": "lottery"},
        ],
    },
    "4": {
        "key": "mod",
        "name": "群管功能",
        "desc": "违禁词、禁言、惩罚、公告、数据",
        "emoji": "4️⃣",
        "entries": [
            {"title": "违禁词列表", "action": "banned_list"},
            {"title": "公告列表", "action": "announcement_list"},
            {"title": "群数据", "action": "group_stats"},
            {"title": "活跃榜", "action": "active_rank"},
            {"title": "违规榜", "action": "violation_rank"},
            {"title": "白名单列表", "action": "whitelist_list"},
            {"title": "平台信息", "action": "platform_info"},
            {"title": "惩罚记录", "action": "punish_logs"},
            {"title": "手动惩罚", "action": "", "usage": "/手动惩罚 @成员 警告|扣分|黑名单"},
            {"title": "积分惩罚", "action": "", "usage": "/积分惩罚 @成员 数量 [原因]"},
            {"title": "发布公告", "action": "", "usage": "/发布公告 内容"},
            {"title": "禁言成员", "action": "", "usage": "/禁言 @成员 分钟"},
            {"title": "解除禁言", "action": "", "usage": "/解除禁言 @成员"},
            {"title": "触发方式", "action": "trigger_mode_show"},
        ],
    },
    "5": {
        "key": "me",
        "name": "个人中心",
        "desc": "我的信息、记录、任务、帮助",
        "emoji": "5️⃣",
        "entries": [
            {"title": "我的信息", "action": "my_info"},
            {"title": "积分明细", "action": "points_logs"},
            {"title": "兑换记录", "action": "exchange_logs"},
            {"title": "抽奖记录", "action": "lottery_logs"},
            {"title": "转账记录", "action": "transfer_logs"},
            {"title": "我的任务", "action": "tasks"},
            {"title": "我的成就", "action": "achievements"},
            {"title": "查看触发方式", "action": "trigger_mode_show"},
        ],
    },
    "6": {
        "key": "admin",
        "name": "管理后台",
        "desc": "所有管理员指令",
        "emoji": "6️⃣",
        "entries": [
            {"title": "添加商品", "action": "", "usage": "/添加商品 名称 所需积分 库存"},
            {"title": "删除商品", "action": "", "usage": "/删除商品 编号或名称"},
            {"title": "商城管理", "action": "", "usage": "/商城管理"},
            {"title": "添加奖品", "action": "", "usage": "/添加奖品 名称 概率 库存"},
            {"title": "删除奖品", "action": "", "usage": "/删除奖品 编号或名称"},
            {"title": "抽奖统计", "action": "", "usage": "/抽奖统计"},
            {"title": "添加任务", "action": "", "usage": "/添加任务 名称 类型 目标 积分 经验"},
            {"title": "删除任务", "action": "", "usage": "/删除任务 编号或名称"},
            {"title": "加积分", "action": "", "usage": "/加积分 @成员 数量"},
            {"title": "减积分", "action": "", "usage": "/减积分 @成员 数量"},
            {"title": "设置签到奖励", "action": "", "usage": "/设置签到奖励 数量"},
            {"title": "签到提醒", "action": "", "usage": "/开启签到提醒 / /关闭签到提醒"},
            {"title": "设置触发方式", "action": "", "usage": "/触发模式 全部|指令|关键词"},
        ],
    },
}

# 功能详细说明：键为功能名，值为 (用法, 说明, 所属分类)
SMART_FUNCTION_HELP: dict[str, tuple[str, str, str]] = {
    "签到": ("/签到", "每天签到一次，获得积分与经验；连续签到有额外奖励。", "签到积分"),
    "查询积分": ("/积分", "查看当前积分、经验、等级与称号进度。", "签到积分"),
    "排行榜": ("/排行榜", "查看本群积分排行榜前 10 名。", "签到积分"),
    "积分明细": ("/积分明细", "查看最近 20 条积分变动记录。", "签到积分"),
    "查看商城": ("/商城", "查看本群商品、所需积分与库存，商品编号用于兑换。", "商城兑换"),
    "兑换商品": ("/兑换 商品编号或名称", "消耗积分兑换商品，库存自动扣减。", "商城兑换"),
    "兑换记录": ("/兑换记录", "查看个人最近 20 条兑换记录。", "商城兑换"),
    "查看奖池": ("/奖池", "查看抽奖奖品、概率与剩余库存。", "商城兑换"),
    "抽奖一次": (f"/抽奖（消耗 {LOTTERY_COST} 积分）", f"消耗 {LOTTERY_COST} 积分抽奖一次，连续 {LOTTERY_PITY} 次未中奖必中。", "商城兑换"),
    "抽奖记录": ("/抽奖记录", "查看个人最近 20 条抽奖记录。", "商城兑换"),
    "我的等级": ("/我的信息", "查看积分、经验、等级、称号与升级所需经验。", "等级成就"),
    "我的成就": ("/成就", "查看个人已达成成就与奖励。", "等级成就"),
    "全部成就": ("/成就列表", "查看全部成就及达成条件。", "等级成就"),
    "今日任务": ("/任务", "查看今日任务完成进度与奖励。", "等级成就"),
    "我的任务": ("/任务", "查看今日任务完成进度与奖励。", "等级成就"),
    "任务列表": ("/任务列表", "查看本群全部每日任务。", "等级成就"),
    "违禁词列表": ("/违禁词列表", "查看本群违禁词与对应惩罚；管理员可添加/删除。", "群管功能"),
    "公告列表": ("/公告列表", "查看历史公告与定时公告状态。", "群管功能"),
    "群数据": ("/群数据", "查看总人数、今日活跃、发言、违规与积分总量。", "群管功能"),
    "活跃榜": ("/活跃榜", "查看今日发言活跃度排名前 10。", "群管功能"),
    "违规榜": ("/违规榜", "查看成员违规次数排名前 10。", "群管功能"),
    "白名单列表": ("/白名单列表", "查看免受违禁词与自动审核检测的成员。", "群管功能"),
    "发布公告": ("/发布公告 内容", "发布群公告并 @全体成员。", "群管功能"),
    "禁言成员": ("/禁言 @成员 分钟", "手动禁言成员；平台不支持时自动降级为警告 + 扣分。", "群管功能"),
    "解除禁言": ("/解除禁言 @成员", "解除成员禁言；QQ 官方机器人不支持。", "群管功能"),
    "平台信息": ("/平台信息", "查看当前平台、禁言/踢人/撤回能力与降级策略。", "群管功能"),
    "惩罚记录": ("/惩罚记录 [@成员]", "查看自己或指定成员的警告与惩罚历史。", "群管功能"),
    "手动惩罚": ("/手动惩罚 @成员 警告|扣分|黑名单", "手动执行警告、扣分或标记黑名单，管理员可用。", "群管功能"),
    "积分惩罚": ("/积分惩罚 @成员 数量 [原因]", "手动扣除成员积分并记录惩罚，管理员可用。", "群管功能"),
    "触发方式": ("/触发模式 全部|指令|关键词", "设置本群免 @ 触发方式，管理员可用。", "群管功能"),
    "我的信息": ("/我的信息", "查看个人积分、经验、等级、称号与违规记录。", "个人中心"),
    "转账记录": ("/转账记录", "查看个人最近 20 条转账记录。", "个人中心"),
    "添加商品": ("/添加商品 名称 所需积分 库存", "管理员添加商城商品。", "管理后台"),
    "删除商品": ("/删除商品 编号或名称", "管理员删除商城商品，支持编号或完整名称。", "管理后台"),
    "商城管理": ("/商城管理", "管理员查看商品与兑换统计。", "管理后台"),
    "添加奖品": ("/添加奖品 名称 概率 库存", "管理员添加抽奖奖品。", "管理后台"),
    "删除奖品": ("/删除奖品 编号或名称", "管理员删除抽奖奖品，支持编号或完整名称。", "管理后台"),
    "抽奖统计": ("/抽奖统计", "管理员查看抽奖数据统计。", "管理后台"),
    "添加任务": ("/添加任务 名称 类型 目标 积分 经验", "管理员添加每日任务。", "管理后台"),
    "删除任务": ("/删除任务 编号或名称", "管理员删除每日任务，支持编号或完整名称。", "管理后台"),
    "加积分": ("/加积分 @成员 数量", "管理员手动增加成员积分。", "管理后台"),
    "减积分": ("/减积分 @成员 数量", "管理员手动减少成员积分。", "管理后台"),
    "设置签到奖励": ("/设置签到奖励 数量", "管理员设置本群签到基础积分。", "管理后台"),
    "签到提醒": ("/开启签到提醒 或 /关闭签到提醒", "管理员控制每天 08:00 的签到提醒。", "管理后台"),
    "设置触发方式": ("/触发模式 全部|指令|关键词", "管理员设置本群免 @ 触发方式。", "管理后台"),
}

# 主菜单快捷入口
SMART_QUICK_ENTRIES = ("签到", "积分", "商城", "排行榜", "抽奖", "帮助")

# 智能触发整体匹配所有非空消息（真正的关键词判断在代码中完成）
SMART_TRIGGER_PATTERN = r"^[\s\S]+$"

# 消息前缀：/ 交给 AstrBot 指令系统；！ 由智能触发兼容；
# # 默认作为聊天模式前缀（可在 config.yaml 的 chat_prefixes 中调整）
TRIGGER_PREFIXES = ("/", "／", "#", "＃", "!", "！")

# 上下文类型
CONTEXT_MENU = "menu"
CONTEXT_CATEGORY = "category"
CONTEXT_SHOP = "shop"
CONTEXT_CHAT = "chat"
CONTEXT_CONFIRM = "confirm"


class AhoCorasick:
    """Aho-Corasick 多模式字符串匹配自动机。

    用于一次性扫描消息文本，找出所有命中的违禁词，时间复杂度为
    ``O(len(text) + 命中次数)``，与违禁词数量基本无关。插件按群缓存
    自动机，只有在违禁词增删时才重建。
    """

    def __init__(self, words: list[str] | tuple[str, ...] = ()):
        # goto：状态转移表；fail：失配指针；output：该状态对应的模式串
        self._goto: list[dict[str, int]] = [{}]
        self._fail: list[int] = [0]
        self._output: list[list[str]] = [[]]
        for word in words:
            self.add(word)
        self._build()

    def add(self, word: str) -> None:
        """向自动机中加入一个模式串（需在 build 前调用）。"""
        word = str(word or "").strip().lower()
        if not word:
            return
        state = 0
        for char in word:
            next_state = self._goto[state].get(char)
            if next_state is None:
                next_state = len(self._goto)
                self._goto[state][char] = next_state
                self._goto.append({})
                self._fail.append(0)
                self._output.append([])
            state = next_state
        if word not in self._output[state]:
            self._output[state].append(word)

    def _build(self) -> None:
        """按 BFS 构建失配指针，并把输出沿失配链合并。"""
        queue: deque[int] = deque()
        for state in self._goto[0].values():
            self._fail[state] = 0
            queue.append(state)

        while queue:
            state = queue.popleft()
            for char, next_state in self._goto[state].items():
                fail = self._fail[state]
                while fail and char not in self._goto[fail]:
                    fail = self._fail[fail]
                self._fail[next_state] = self._goto[fail].get(char, 0)
                if self._fail[next_state] == next_state:
                    self._fail[next_state] = 0
                self._output[next_state].extend(
                    self._output[self._fail[next_state]],
                )
                queue.append(next_state)

    def search(self, text: str) -> list[str]:
        """返回文本中命中的全部模式串（去重，按命中顺序）。"""
        if not text:
            return []
        text = text.lower()
        state = 0
        hits: list[str] = []
        seen: set[str] = set()
        for char in text:
            while state and char not in self._goto[state]:
                state = self._fail[state]
            state = self._goto[state].get(char, 0)
            for word in self._output[state]:
                if word not in seen:
                    seen.add(word)
                    hits.append(word)
        return hits


class KeywordTrie:
    """关键词 Trie 树，用于智能免 @ 触发。

    与 Aho-Corasick 不同，这里只需要“找出文本中出现的最长关键词”，
    因此不构建失配指针：对每个起始位置沿树向下走，记录最深的命中。
    整体复杂度为 ``O(len(text) * 最长关键词长度)``，对聊天短句足够快。
    """

    def __init__(self, mapping: dict[str, str]):
        # children：字符 -> 子节点；action：该节点对应的动作键
        self._children: list[dict[str, int]] = [{}]
        self._action: list[str] = [""]
        for word, action in mapping.items():
            self.insert(word, action)

    def insert(self, word: str, action: str) -> None:
        """插入关键词与其动作键。"""
        word = str(word or "").strip().lower()
        if not word:
            return
        node = 0
        for char in word:
            nxt = self._children[node].get(char)
            if nxt is None:
                nxt = len(self._children)
                self._children[node][char] = nxt
                self._children.append({})
                self._action.append("")
            node = nxt
        self._action[node] = action

    def find_longest(self, text: str) -> tuple[str, str]:
        """返回文本中出现的最长关键词及其动作键，未命中返回 ("", "")。"""
        if not text:
            return "", ""
        text = text.lower()
        best_word = ""
        best_action = ""
        for start in range(len(text)):
            node = 0
            for pos in range(start, len(text)):
                node = self._children[node].get(text[pos])
                if node is None:
                    break
                action = self._action[node]
                if action:
                    word = text[start : pos + 1]
                    # 采用最长匹配；长度相同时保留先出现的
                    if len(word) > len(best_word):
                        best_word = word
                        best_action = action
        return best_word, best_action


class ClassManagerPlugin(Star):
    """班级管理插件主类。"""

    def __init__(self, context: Context):
        super().__init__(context)
        # 数据库在插件加载时初始化，数据保存在插件目录的 data/ 下
        self.db = ClassDatabase(DB_PATH)
        self._reminder_task: asyncio.Task | None = None
        self._announcement_task: asyncio.Task | None = None

        # v4.0.0 群管缓存与运行状态（缓存避免每条消息都查询数据库）
        self._banned_cache: dict[str, tuple[list[str], AhoCorasick]] = {}
        self._mod_cache: dict[str, dict[str, dict[str, Any]]] = {}
        self._whitelist_cache: dict[str, set[str]] = {}
        self._spam_state: dict[tuple[str, str], deque[float]] = {}
        self._auto_cooldown: dict[tuple[str, str, str], float] = {}
        self._bg_tasks: set[asyncio.Task] = set()

        # v1.5.0 智能交互运行状态（v1.5.1 继续沿用）
        self._config_cache: dict[str, Any] = dict(DEFAULT_PLUGIN_CONFIG)
        self._config_mtime: float = -1.0  # config.yaml 最后修改时间，用于热更新
        self._keyword_trie = KeywordTrie(SMART_KEYWORDS)
        self._contexts: dict[tuple[str, str], dict[str, Any]] = {}
        self._trigger_times: dict[str, deque[float]] = {}
        self._trigger_hits: dict[str, int] = {}
        self._trigger_cooldowns: dict[str, float] = {}
        self._context_task: asyncio.Task | None = None

        # v1.5.1：群成员身份缓存，避免排行榜每条都调用平台 API
        self._role_cache: dict[tuple[str, str], tuple[str, float]] = {}

        # 立即读取 config.yaml，使配置在插件加载时即生效
        self._reload_config(force=True)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def initialize(self) -> None:
        """插件激活后启动签到提醒与定时公告后台任务。"""
        if self._reminder_task is None or self._reminder_task.done():
            self._reminder_task = asyncio.create_task(self._reminder_loop())
        if self._announcement_task is None or self._announcement_task.done():
            self._announcement_task = asyncio.create_task(
                self._announcement_loop(),
            )
        if self._context_task is None or self._context_task.done():
            self._context_task = asyncio.create_task(self._context_cleanup_loop())
        self.logger.info("班级管理插件 v1.5.4 已启动，数据库：%s", DB_PATH)

    async def terminate(self) -> None:
        """插件停用/重载时取消后台任务并关闭数据库。"""
        if self._reminder_task is not None:
            self._reminder_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._reminder_task
            self._reminder_task = None

        if self._announcement_task is not None:
            self._announcement_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._announcement_task
            self._announcement_task = None

        if self._context_task is not None:
            self._context_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._context_task
            self._context_task = None

        # 取消仍在执行的自动审核任务
        for task in list(self._bg_tasks):
            task.cancel()
        self._bg_tasks.clear()

        try:
            self.db.close()
        except Exception:
            self.logger.warning("关闭数据库时出现异常", exc_info=True)

    # ------------------------------------------------------------------
    # 配置管理（config.yaml 热更新）
    # ------------------------------------------------------------------
    def _reload_config(self, force: bool = False) -> dict[str, Any]:
        """读取 config.yaml；文件变化时自动重新加载（热更新）。

        每次触发智能交互时调用，通过文件修改时间判断是否需要重新解析，
        因此管理员直接编辑 config.yaml 后无需重启插件即可生效。
        """
        try:
            mtime = CONFIG_PATH.stat().st_mtime
        except FileNotFoundError:
            mtime = -1.0
        except OSError:
            mtime = self._config_mtime

        if not force and mtime == self._config_mtime:
            return self._config_cache

        raw: dict[str, Any] = {}
        if mtime >= 0:
            try:
                content = CONFIG_PATH.read_text(encoding="utf-8")
                if yaml is not None:
                    loaded = yaml.safe_load(content)
                    raw = loaded if isinstance(loaded, dict) else {}
                else:
                    raw = self._parse_simple_yaml(content)
            except Exception:
                self.logger.warning(
                    "读取 config.yaml 失败，继续使用上一份配置",
                    exc_info=True,
                )
                raw = dict(self._config_cache)

        config = dict(DEFAULT_PLUGIN_CONFIG)

        # 触发方式
        mode = str(raw.get("trigger_mode", config["trigger_mode"])).strip().lower()
        if mode in ("all", "command", "keyword"):
            config["trigger_mode"] = mode

        # 菜单样式
        style = str(raw.get("menu_style", config["menu_style"])).strip().lower()
        if style in ("category", "flat"):
            config["menu_style"] = style

        # 冷却秒数
        try:
            cooldown = int(raw.get("cooldown_seconds", config["cooldown_seconds"]))
            config["cooldown_seconds"] = max(0, min(60, cooldown))
        except (TypeError, ValueError):
            pass

        # 上下文超时秒数
        try:
            timeout = int(raw.get("context_timeout", config["context_timeout"]))
            config["context_timeout"] = max(5, min(600, timeout))
        except (TypeError, ValueError):
            pass

        # v1.5.1：会话超时（兼容旧的 context_timeout）
        try:
            session_timeout = int(
                raw.get(
                    "session_timeout",
                    raw.get("context_timeout", config["session_timeout"]),
                ),
            )
            config["session_timeout"] = max(5, min(600, session_timeout))
        except (TypeError, ValueError):
            pass

        # 聊天模式超时
        try:
            chat_timeout = int(raw.get("chat_timeout", config["chat_timeout"]))
            config["chat_timeout"] = max(5, min(600, chat_timeout))
        except (TypeError, ValueError):
            pass

        # v1.5.1 布尔开关
        config["admin_bypass"] = self._as_bool(
            raw.get("admin_bypass", config["admin_bypass"]),
            bool(config["admin_bypass"]),
        )
        config["block_llm"] = self._as_bool(
            raw.get("block_llm", config["block_llm"]),
            bool(config["block_llm"]),
        )
        config["smart_menu"] = self._as_bool(
            raw.get("smart_menu", config["smart_menu"]),
            bool(config["smart_menu"]),
        )
        config["identity_display"] = self._as_bool(
            raw.get("identity_display", config["identity_display"]),
            bool(config["identity_display"]),
        )
        config["auto_detect"] = self._as_bool(
            raw.get("auto_detect", config["auto_detect"]),
            bool(config["auto_detect"]),
        )

        # 聊天模式前缀
        prefixes = raw.get("chat_prefixes", config["chat_prefixes"])
        if isinstance(prefixes, str):
            prefixes = re.split(r"[,，\s]+", prefixes)
        if isinstance(prefixes, (list, tuple)):
            cleaned = [str(item).strip() for item in prefixes if str(item).strip()]
            if cleaned:
                config["chat_prefixes"] = cleaned

        # v1.5.3：惩罚配置（嵌套 punishment 段）
        defaults = DEFAULT_PLUGIN_CONFIG["punishment"]
        raw_punish = raw.get("punishment")
        if not isinstance(raw_punish, dict):
            raw_punish = {}
        punishment = dict(defaults)
        punishment["auto_mute"] = self._as_bool(
            raw_punish.get("auto_mute", defaults["auto_mute"]),
            bool(defaults["auto_mute"]),
        )
        punishment["fallback_to_warning"] = self._as_bool(
            raw_punish.get("fallback_to_warning", defaults["fallback_to_warning"]),
            bool(defaults["fallback_to_warning"]),
        )
        try:
            mute_duration = int(
                raw_punish.get("mute_duration", defaults["mute_duration"]),
            )
            punishment["mute_duration"] = max(1, min(MAX_MUTE_MINUTES, mute_duration))
        except (TypeError, ValueError):
            pass
        try:
            threshold = int(
                raw_punish.get(
                    "warning_threshold",
                    defaults["warning_threshold"],
                ),
            )
            punishment["warning_threshold"] = max(1, min(20, threshold))
        except (TypeError, ValueError):
            pass
        try:
            deduction = int(
                raw_punish.get("points_deduction", defaults["points_deduction"]),
            )
            punishment["points_deduction"] = max(0, min(1000, deduction))
        except (TypeError, ValueError):
            pass
        config["punishment"] = punishment

        self._config_cache = config
        self._config_mtime = mtime
        return config

    @staticmethod
    def _as_bool(value: Any, default: bool = False) -> bool:
        """把 YAML/文本中的布尔值统一转换为 bool。"""
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on", "是", "开")
        if value is None:
            return default
        return bool(value)

    @staticmethod
    def _parse_simple_yaml(content: str) -> dict[str, Any]:
        """在缺少 PyYAML 时解析简单的 ``key: value`` 与一层嵌套配置。

        支持：
        - 标量：字符串、整数、true/false；
        - 行内列表：``[a, b, c]``；
        - 一层嵌套：
          ``punishment:`` 下一行起缩进两格的 ``auto_mute: true`` 等字段。
        """
        result: dict[str, Any] = {}
        current: dict[str, Any] | None = None
        for raw_line in content.splitlines():
            if not raw_line.strip() or raw_line.lstrip().startswith("#"):
                continue
            # 只把“空格 + #”视为注释，避免误截 chat_prefixes 中的 "#"
            comment_index = raw_line.find(" #")
            if comment_index >= 0:
                raw_line = raw_line[:comment_index]
            line = raw_line.rstrip()
            if ":" not in line:
                continue
            indent = len(raw_line) - len(raw_line.lstrip())
            key, value = line.split(":", 1)
            key = key.strip()
            value = value.strip()
            if not key:
                continue

            if indent > 0 and current is not None:
                current[key] = ClassManagerPlugin._parse_scalar(value)
                continue

            if value == "":
                # 顶层空值视为嵌套配置段的开始
                current = {}
                result[key] = current
            else:
                current = None
                result[key] = ClassManagerPlugin._parse_scalar(value)
        return result

    @staticmethod
    def _parse_scalar(value: str) -> Any:
        """解析简易 YAML 的标量/行内列表。"""
        value = str(value or "").strip()
        if value.startswith("[") and value.endswith("]"):
            inner = value[1:-1].strip()
            if not inner:
                return []
            return [
                item.strip().strip("'\"")
                for item in re.split(r"[,，]", inner)
                if item.strip()
            ]
        value = value.strip("'\"")
        lower = value.lower()
        if lower in ("true", "false"):
            return lower == "true"
        try:
            return int(value)
        except ValueError:
            return value

    def _effective_trigger_mode(self, group_id: str) -> str:
        """获取本群实际触发方式：群设置优先，其次全局配置。"""
        mode = ""
        try:
            mode = self.db.get_group_trigger_mode(group_id)
        except Exception:
            self.logger.debug("读取群触发方式失败", exc_info=True)
        if mode in ("all", "command", "keyword"):
            return mode
        return str(self._reload_config().get("trigger_mode", "all"))

    async def _context_cleanup_loop(self) -> None:
        """定期清理过期的上下文与频率限制状态，防止内存无限增长。"""
        while True:
            try:
                await asyncio.sleep(60)
                now = time.monotonic()
                # 清理过期上下文
                for key, item in list(self._contexts.items()):
                    if float(item.get("expires", 0)) <= now:
                        self._contexts.pop(key, None)
                # 清理冷却已结束的限流状态
                for user_id, until in list(self._trigger_cooldowns.items()):
                    if until <= now:
                        self._trigger_cooldowns.pop(user_id, None)
                        self._trigger_times.pop(user_id, None)
                        self._trigger_hits.pop(user_id, None)
                # 清理过期身份缓存（5 分钟）
                for key, (_role, expires) in list(self._role_cache.items()):
                    if expires <= now:
                        self._role_cache.pop(key, None)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.warning(
                    "清理智能交互缓存时出现异常：\n%s",
                    traceback.format_exc(),
                )

    # ------------------------------------------------------------------
    # 签到
    # ------------------------------------------------------------------
    @filter.command("签到", alias={"打卡"})
    async def sign_in_command(self, event: AstrMessageEvent):
        """每日签到（/签到 或 签到）。"""
        if not self._claim_event(event):
            return
        async for result in self._run_safely(
            event,
            self._handle_sign_in,
            action="签到",
        ):
            yield result

    async def _handle_sign_in(
        self,
        event: AstrMessageEvent,
    ) -> AsyncGenerator[Any, None]:
        """签到逻辑：每天一次，连续签到有额外奖励，同时获得经验。"""
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 签到只能在群聊中使用哦～")
            return

        user_id = str(event.get_sender_id() or "").strip()
        if not user_id:
            yield event.plain_result("❌ 无法获取你的用户 ID，签到失败。")
            return

        nickname = event.get_sender_name() or ""
        self._touch_group_and_user(event, group_id, user_id, nickname)

        base_reward = self.db.get_sign_reward(group_id)
        result = self.db.sign_in(
            group_id=group_id,
            user_id=user_id,
            nickname=nickname,
            base_reward=base_reward,
        )
        config = self._reload_config()
        identity_line = ""
        if config.get("identity_display", True):
            role = await self._get_member_role(event, group_id, user_id)
            identity_line = f"\n身份：{self._identity_text(role)}"

        if result["already_signed"]:
            yield event.plain_result(
                "❌ 你今天已经签到过了，明天再来吧～\n"
                f"当前积分：{self._fmt_points(result['points'])}\n"
                f"连续签到：{result['streak']} 天\n"
                f"当前等级：Lv.{result['level']} {result['title']}"
                f"{identity_line}",
            )
            return

        lines = [
            f"✅ 签到成功！获得积分：+{self._fmt_points(result['gained'])}",
            f"当前积分：{self._fmt_points(result['points'])}",
            f"连续签到：{result['streak']} 天",
            f"✨ 获得经验：+{result['exp_gained']}",
            f"🏅 当前等级：Lv.{result['level']} {result['title']}",
        ]
        if identity_line:
            lines.append(identity_line.lstrip("\n"))
        for label, bonus in result["bonus_details"]:
            lines.append(f"🎁 {label}奖励：+{bonus}")
        if result["level_up"]:
            lines.append(
                f"🎉 恭喜升级！Lv.{result['old_level']} → Lv.{result['level']}",
            )
            if result["old_title"] != result["title"]:
                lines.append(f"🎖️ 解锁新称号：{result['title']}")

        # v3.0.0：结算每日任务与成就
        lines.extend(self._settle_rewards(group_id, user_id, nickname))

        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------------------------
    # 积分查询
    # ------------------------------------------------------------------
    @filter.command("积分", alias={"我的积分", "查询积分"})
    async def my_points_command(self, event: AstrMessageEvent):
        """查看自己的积分（/积分 或 我的积分）。"""
        if not self._claim_event(event):
            return
        async for result in self._run_safely(
            event,
            self._handle_my_points,
            action="查询积分",
        ):
            yield result

    async def _handle_my_points(
        self,
        event: AstrMessageEvent,
    ) -> AsyncGenerator[Any, None]:
        """展示当前用户在群内的积分、签到次数、连续天数和排名。"""
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 积分查询只能在群聊中使用哦～")
            return

        user_id = str(event.get_sender_id() or "").strip()
        if not user_id:
            yield event.plain_result("❌ 无法获取你的用户 ID。")
            return

        self._touch_group_and_user(
            event,
            group_id,
            user_id,
            event.get_sender_name() or "",
        )

        user = self.db.get_user(group_id, user_id) or {}
        points = round(float(user.get("points", 0) or 0), 2)
        total_signs = int(user.get("total_signs", 0) or 0)
        streak = int(user.get("streak", 0) or 0)
        rank = self.db.get_rank(group_id, points)

        yield event.plain_result(
            "📊 我的积分\n"
            f"当前积分：{self._fmt_points(points)}\n"
            f"总签到次数：{total_signs} 次\n"
            f"连续签到：{streak} 天\n"
            f"群排名：第 {rank} 名",
        )

    # ------------------------------------------------------------------
    # 我的信息（积分 + 经验 + 等级 + 称号 + 进度）
    # ------------------------------------------------------------------
    @filter.command("我的信息", alias={"个人信息"})
    async def my_info_command(self, event: AstrMessageEvent):
        """查看积分、经验、等级、称号与升级进度。"""
        if not self._claim_event(event):
            return
        async for result in self._run_safely(
            event,
            self._handle_my_info,
            action="查询我的信息",
        ):
            yield result

    async def _handle_my_info(
        self,
        event: AstrMessageEvent,
    ) -> AsyncGenerator[Any, None]:
        """完整展示身份、积分、经验、等级、称号与升级进度。"""
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 我的信息只能在群聊中查看哦～")
            return

        user_id = str(event.get_sender_id() or "").strip()
        if not user_id:
            yield event.plain_result("❌ 无法获取你的用户 ID。")
            return

        self._touch_group_and_user(
            event,
            group_id,
            user_id,
            event.get_sender_name() or "",
        )

        user = self.db.get_user(group_id, user_id) or {}
        points = round(float(user.get("points", 0) or 0), 2)
        total_signs = int(user.get("total_signs", 0) or 0)
        streak = int(user.get("streak", 0) or 0)
        exp = int(user.get("exp", 0) or 0)
        progress = level_progress(exp)
        rank = self.db.get_rank(group_id, points)
        name = self._display_name(
            user.get("nickname") or event.get_sender_name(),
            user_id,
        )
        overview = self.db.get_user_overview(group_id, user_id)
        role = await self._get_member_role(event, group_id, user_id)
        config = self._reload_config()
        joined = str(user.get("created_at") or "").strip()[:10] or "未知"

        lines = ["📊 我的信息", ""]
        if config.get("identity_display", True):
            lines.extend(
                [
                    "【身份信息】",
                    f"昵称：{name}",
                    f"身份：{self._identity_text(role)}",
                    f"加入时间：{joined}",
                    "",
                ],
            )

        lines.extend(
            [
                "【积分信息】",
                f"当前积分：{self._fmt_points(points)}",
                f"总签到次数：{total_signs} 次",
                f"连续签到：{streak} 天",
                f"群排名：第 {rank} 名",
                "",
                "【等级信息】",
                f"当前等级：Lv.{progress['level']}（称号：{progress['title']}）",
                f"经验值：{exp}/{exp + progress['remaining']}",
                f"升级进度：{progress['percent']:.0f}%",
                f"{self._progress_bar(progress['percent'])}",
                "",
                "【其他】",
                f"兑换次数：{overview['exchange_count']} 次",
                f"抽奖次数：{overview['lottery_count']} 次",
                f"违规记录：{overview['violation_count']} 次",
                "",
            ],
        )

        if role in ("owner", "admin"):
            lines.append("管理入口：发送“/帮助 管理后台”查看管理指令")
        else:
            lines.append("管理权限：普通成员（管理指令不可用）")

        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------------------------
    # 排行榜
    # ------------------------------------------------------------------
    @filter.command("排行榜", alias={"积分排行", "排行"})
    async def leaderboard_command(self, event: AstrMessageEvent):
        """查看班级积分排行榜（/排行榜 或 积分排行）。"""
        if not self._claim_event(event):
            return
        async for result in self._run_safely(
            event,
            self._handle_leaderboard,
            action="查询排行榜",
        ):
            yield result

    async def _handle_leaderboard(
        self,
        event: AstrMessageEvent,
    ) -> AsyncGenerator[Any, None]:
        """展示本群积分前 10 名。"""
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 排行榜只能在群聊中查看哦～")
            return

        rows = self.db.get_leaderboard(group_id, 10)
        lines = ["🏆 班级积分排行榜"]
        config = self._reload_config()
        show_identity = bool(config.get("identity_display", True))
        if not rows:
            lines.append("暂无积分记录，快来签到吧！")
        else:
            for index, row in enumerate(rows, start=1):
                name = self._display_name(row.get("nickname"), row["user_id"])
                badge = ""
                if show_identity:
                    role = await self._get_member_role(
                        event,
                        group_id,
                        str(row["user_id"]),
                    )
                    badge = f"{self._identity_text(role)} "
                lines.append(
                    f"{index}. {badge}{name} - "
                    f"{self._fmt_points(row['points'])} 分",
                )

        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------------------------
    # 积分商城
    # ------------------------------------------------------------------
    @filter.command("商城", alias={"积分商城"})
    async def shop_command(self, event: AstrMessageEvent):
        """查看本群积分商城。"""
        if not self._claim_event(event):
            return
        async for result in self._run_safely(
            event,
            self._handle_shop,
            action="查看商城",
        ):
            yield result

    async def _handle_shop(
        self,
        event: AstrMessageEvent,
    ) -> AsyncGenerator[Any, None]:
        """展示本群商品列表，库存为 0 的商品标记为已售罄。"""
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 商城只能在群聊中查看哦～")
            return

        items = self.db.list_shop_items(group_id)
        lines = ["🛒 班级积分商城"]
        if not items:
            lines.append("暂无商品，请联系管理员添加。")
        else:
            for item in items[:20]:
                stock = int(item["stock"] or 0)
                status = f"库存 {stock}" if stock > 0 else "已售罄"
                lines.append(
                    f"[#{item['id']}] {item['name']} - "
                    f"{self._fmt_points(item['cost'])} 积分（{status}）",
                )
            if len(items) > 20:
                lines.append(f"…… 还有 {len(items) - 20} 件商品未显示")
            lines.append("")
            lines.append("发送 /兑换 商品编号或名称 即可兑换")

        yield event.plain_result("\n".join(lines))

    @filter.command("添加商品")
    async def add_shop_item_command(self, event: AstrMessageEvent):
        """添加商城商品，仅群主/管理员可用。"""
        if not self._claim_event(event):
            return
        async for result in self._run_safely(
            event,
            self._handle_add_shop_item,
            action="添加商品",
        ):
            yield result

    async def _handle_add_shop_item(
        self,
        event: AstrMessageEvent,
    ) -> AsyncGenerator[Any, None]:
        """解析“/添加商品 名称 所需积分 库存”并添加商品。"""
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 该指令只能在群聊中使用。")
            return
        if not await self._is_group_admin(event):
            yield event.plain_result("❌ 该指令仅限群主/管理员使用。")
            return

        remainder = self._command_remainder(event, "添加商品")
        parts = remainder.rsplit(None, 2)
        if len(parts) != 3:
            yield event.plain_result(
                "❌ 格式错误，请使用：/添加商品 名称 所需积分 库存\n"
                "例如：/添加商品 免作业券 50 10",
            )
            return

        name, cost_raw, stock_raw = (part.strip() for part in parts)
        if not name or len(name) > 30:
            yield event.plain_result("❌ 商品名称不能为空，且不能超过 30 个字符。")
            return

        try:
            cost = int(cost_raw)
            stock = int(stock_raw)
        except (TypeError, ValueError):
            yield event.plain_result("❌ 所需积分和库存必须是整数。")
            return

        if cost <= 0 or cost > 1_000_000:
            yield event.plain_result("❌ 所需积分必须是 1~1000000 之间的整数。")
            return
        if stock < 0 or stock > 1_000_000:
            yield event.plain_result("❌ 库存必须是 0~1000000 之间的整数。")
            return

        result = self.db.add_shop_item(group_id, name, cost, stock)
        if not result["ok"]:
            yield event.plain_result(
                f"❌ 本群已存在同名商品（编号：#{result['item_id']}）。",
            )
            return

        yield event.plain_result(
            "✅ 商品添加成功\n"
            f"编号：#{result['item_id']}\n"
            f"名称：{result['name']}\n"
            f"所需积分：{result['cost']}\n"
            f"库存：{result['stock']}",
        )

    @filter.command("删除商品")
    async def delete_shop_item_command(
        self,
        event: AstrMessageEvent,
        item_id: str = "",
    ):
        """删除商城商品，仅群主/管理员可用。"""
        if not self._claim_event(event):
            return
        async for result in self._run_safely(
            event,
            self._handle_delete_shop_item,
            item_id,
            action="删除商品",
        ):
            yield result

    async def _handle_delete_shop_item(
        self,
        event: AstrMessageEvent,
        raw_item_id: str = "",
    ) -> AsyncGenerator[Any, None]:
        """删除指定编号的商品，历史兑换记录仍会保留。"""
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 该指令只能在群聊中使用。")
            return
        if not await self._is_group_admin(event):
            yield event.plain_result("❌ 该指令仅限群主/管理员使用。")
            return

        raw = raw_item_id or self._command_remainder(event, "删除商品")
        item_id, error = self._resolve_item_reference(
            self.db.list_shop_items(group_id),
            raw,
            "商品",
        )
        if item_id is None:
            yield event.plain_result(error or "❌ 商品编号不合法，请使用：/删除商品 商品编号或名称")
            return

        item = self.db.get_shop_item(group_id, item_id)
        if item is None:
            yield event.plain_result(f"❌ 商品 #{item_id} 不存在。")
            return

        self.db.delete_shop_item(group_id, item_id)
        yield event.plain_result(
            f"✅ 已删除商品 #{item_id}「{item['name']}」\n历史兑换记录仍然保留。",
        )

    @filter.command("兑换")
    async def exchange_command(
        self,
        event: AstrMessageEvent,
        item_id: str = "",
    ):
        """使用积分兑换商品。"""
        if not self._claim_event(event):
            return
        async for result in self._run_safely(
            event,
            self._handle_exchange,
            item_id,
            action="兑换商品",
        ):
            yield result

    async def _handle_exchange(
        self,
        event: AstrMessageEvent,
        raw_item_id: str = "",
    ) -> AsyncGenerator[Any, None]:
        """检查积分与库存后兑换商品，库存自动扣减。"""
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 兑换只能在群聊中使用哦～")
            return

        user_id = str(event.get_sender_id() or "").strip()
        if not user_id:
            yield event.plain_result("❌ 无法获取你的用户 ID，兑换失败。")
            return

        raw = raw_item_id or self._command_remainder(event, "兑换")
        item_id, error = self._resolve_item_reference(
            self.db.list_shop_items(group_id),
            raw,
            "商品",
        )
        if item_id is None:
            yield event.plain_result(
                (error or "❌ 商品编号不合法，请使用：/兑换 商品编号或名称")
                + "\n发送 /商城 可查看商品编号与名称。",
            )
            return

        nickname = event.get_sender_name() or ""
        self._touch_group_and_user(event, group_id, user_id, nickname)

        result = self.db.exchange_item(group_id, user_id, item_id, nickname)
        status = result["status"]

        if status == "not_found":
            yield event.plain_result(
                f"❌ 商品 #{item_id} 不存在，发送 /商城 查看商品编号。",
            )
            return
        if status == "sold_out":
            yield event.plain_result(
                f"😢 商品「{result['item_name']}」已售罄，下次再来吧～",
            )
            return
        if status == "insufficient":
            yield event.plain_result(
                f"❌ 积分不足，无法兑换「{result['item_name']}」\n"
                f"所需积分：{self._fmt_points(result['cost'])}\n"
                f"当前积分：{self._fmt_points(result['points'])}",
            )
            return

        # v3.0.0：兑换后检测“购物狂”等成就
        lines = [
            "✅ 兑换成功！",
            f"商品：{result['item_name']}",
            f"消耗积分：{self._fmt_points(result['cost'])}",
            f"剩余积分：{self._fmt_points(result['points'])}",
            f"剩余库存：{result['remaining_stock']}",
        ]
        lines.extend(
            self._settle_rewards(group_id, user_id, nickname, include_tasks=False),
        )

        yield event.plain_result("\n".join(lines))

    @filter.command("兑换记录")
    async def exchange_logs_command(self, event: AstrMessageEvent):
        """查看个人兑换历史。"""
        if not self._claim_event(event):
            return
        async for result in self._run_safely(
            event,
            self._handle_exchange_logs,
            action="查询兑换记录",
        ):
            yield result

    async def _handle_exchange_logs(
        self,
        event: AstrMessageEvent,
    ) -> AsyncGenerator[Any, None]:
        """展示当前用户最近的 10 条兑换记录。"""
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 兑换记录只能在群聊中查看哦～")
            return

        user_id = str(event.get_sender_id() or "").strip()
        if not user_id:
            yield event.plain_result("❌ 无法获取你的用户 ID。")
            return

        self._touch_group_and_user(
            event,
            group_id,
            user_id,
            event.get_sender_name() or "",
        )

        rows = self.db.get_exchange_logs(group_id, user_id, 10)
        lines = ["🧾 我的兑换记录"]
        if not rows:
            lines.append("暂无兑换记录，发送 /商城 看看有什么好物吧～")
        else:
            for row in rows:
                lines.append(
                    f"{row['created_at']} "
                    f"「{row['item_name']}」 "
                    f"消耗 {self._fmt_points(row['cost'])} 积分",
                )

        yield event.plain_result("\n".join(lines))

    @filter.command("商城管理")
    async def shop_admin_command(self, event: AstrMessageEvent):
        """查看全部商品及兑换情况，仅群主/管理员可用。"""
        if not self._claim_event(event):
            return
        async for result in self._run_safely(
            event,
            self._handle_shop_admin,
            action="查看商城管理",
        ):
            yield result

    async def _handle_shop_admin(
        self,
        event: AstrMessageEvent,
    ) -> AsyncGenerator[Any, None]:
        """展示商品库存与兑换统计。"""
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 该指令只能在群聊中使用。")
            return
        if not await self._is_group_admin(event):
            yield event.plain_result("❌ 该指令仅限群主/管理员使用。")
            return

        rows = self.db.get_shop_overview(group_id)
        lines = ["🏪 商城管理"]
        if not rows:
            lines.append("暂无商品，请使用 /添加商品 名称 所需积分 库存 添加。")
        else:
            total_exchanges = sum(int(row["exchange_count"] or 0) for row in rows)
            total_cost = sum(int(row["total_cost"] or 0) for row in rows)
            lines.append(
                f"共 {len(rows)} 件商品，累计兑换 {total_exchanges} 次，"
                f"累计消耗 {total_cost} 积分",
            )
            for row in rows:
                stock = int(row["stock"] or 0)
                status = f"库存 {stock}" if stock > 0 else "已售罄"
                lines.append(
                    f"[#{row['id']}] {row['name']} | "
                    f"{self._fmt_points(row['cost'])} 积分 | {status} | "
                    f"已兑换 {int(row['exchange_count'] or 0)} 次 | "
                    f"累计 {int(row['total_cost'] or 0)} 积分",
                )

        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------------------------
    # 抽奖系统（v3.0.0）
    # ------------------------------------------------------------------
    @filter.command("抽奖")
    async def lottery_command(self, event: AstrMessageEvent):
        """消耗积分抽奖一次。"""
        if not self._claim_event(event):
            return
        async for result in self._run_safely(
            event,
            self._handle_lottery,
            action="抽奖",
        ):
            yield result

    async def _handle_lottery(
        self,
        event: AstrMessageEvent,
    ) -> AsyncGenerator[Any, None]:
        """执行抽奖，中奖时祝贺，未中奖提示谢谢参与。"""
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 抽奖只能在群聊中使用哦～")
            return

        user_id = str(event.get_sender_id() or "").strip()
        if not user_id:
            yield event.plain_result("❌ 无法获取你的用户 ID，抽奖失败。")
            return

        nickname = event.get_sender_name() or ""
        self._touch_group_and_user(event, group_id, user_id, nickname)

        result = self.db.draw_lottery(group_id, user_id, nickname)
        status = result["status"]

        if status == "insufficient":
            yield event.plain_result(
                "❌ 积分不足，抽奖需要 "
                f"{self._fmt_points(result['cost'])} 积分\n"
                f"当前积分：{self._fmt_points(result['points'])}",
            )
            return
        if status == "no_prize":
            yield event.plain_result(
                "😢 奖池中暂时没有可抽取的奖品，请联系管理员添加。",
            )
            return

        if result["win"]:
            lines = [
                "🎉 恭喜中奖！",
                f"奖品：{result['prize_name']}",
            ]
            if result["guaranteed"]:
                lines.append(
                    f"✨ 触发保底：连续 {LOTTERY_PITY} 次未中奖，本次必中最低奖！",
                )
            lines.extend(
                [
                    f"消耗积分：{self._fmt_points(result['cost'])}",
                    f"剩余积分：{self._fmt_points(result['points'])}",
                    f"剩余库存：{result['remaining_stock']}",
                ],
            )
        else:
            lines = [
                "😢 谢谢参与～",
                f"消耗积分：{self._fmt_points(result['cost'])}",
                f"剩余积分：{self._fmt_points(result['points'])}",
                f"距离保底还差 {result['next_miss_streak']} 次未中奖",
            ]

        lines.extend(
            self._settle_rewards(group_id, user_id, nickname, include_tasks=False),
        )
        yield event.plain_result("\n".join(lines))

    @filter.command("奖池")
    async def lottery_prizes_command(self, event: AstrMessageEvent):
        """查看本群抽奖奖池。"""
        if not self._claim_event(event):
            return
        async for result in self._run_safely(
            event,
            self._handle_lottery_prizes,
            action="查看奖池",
        ):
            yield result

    async def _handle_lottery_prizes(
        self,
        event: AstrMessageEvent,
    ) -> AsyncGenerator[Any, None]:
        """展示奖品名称、概率与剩余库存。"""
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 奖池只能在群聊中查看哦～")
            return

        prizes = self.db.list_lottery_prizes(group_id)
        lines = ["🎁 本群抽奖奖池"]
        if not prizes:
            lines.append("暂无奖品，请联系管理员使用 /添加奖品 添加。")
        else:
            total = 0.0
            for prize in prizes:
                probability = float(prize["probability"])
                total += probability
                stock = int(prize["stock"] or 0)
                status = f"库存 {stock}" if stock > 0 else "已售罄"
                lines.append(
                    f"[#{prize['id']}] {prize['name']} | "
                    f"概率 {self._fmt_points(probability)}% | {status}",
                )
            lines.append("")
            lines.append(f"概率合计：{self._fmt_points(total)}%")
            lines.append(
                f"每次抽奖消耗 {LOTTERY_COST} 积分，"
                f"连续 {LOTTERY_PITY} 次未中奖后必中最低奖",
            )

        yield event.plain_result("\n".join(lines))

    @filter.command("添加奖品")
    async def add_lottery_prize_command(self, event: AstrMessageEvent):
        """添加抽奖奖品，仅群主/管理员可用。"""
        if not self._claim_event(event):
            return
        async for result in self._run_safely(
            event,
            self._handle_add_lottery_prize,
            action="添加奖品",
        ):
            yield result

    async def _handle_add_lottery_prize(
        self,
        event: AstrMessageEvent,
    ) -> AsyncGenerator[Any, None]:
        """解析“/添加奖品 名称 概率 库存”并添加奖品。"""
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 该指令只能在群聊中使用。")
            return
        if not await self._is_group_admin(event):
            yield event.plain_result("❌ 该指令仅限群主/管理员使用。")
            return

        remainder = self._command_remainder(event, "添加奖品")
        parts = remainder.rsplit(None, 2)
        if len(parts) != 3:
            yield event.plain_result(
                "❌ 格式错误，请使用：/添加奖品 名称 概率 库存\n"
                "例如：/添加奖品 免作业券 10 5",
            )
            return

        name, probability_raw, stock_raw = (part.strip() for part in parts)
        if not name or len(name) > 30:
            yield event.plain_result("❌ 奖品名称不能为空，且不能超过 30 个字符。")
            return

        try:
            probability = float(probability_raw.rstrip("%"))
            stock = int(stock_raw)
        except (TypeError, ValueError):
            yield event.plain_result("❌ 概率和库存必须是数字。")
            return

        if probability <= 0 or probability > 100:
            yield event.plain_result("❌ 中奖概率必须大于 0 且不超过 100（百分比）。")
            return
        if stock < 0 or stock > 1_000_000:
            yield event.plain_result("❌ 库存必须是 0~1000000 之间的整数。")
            return

        result = self.db.add_lottery_prize(group_id, name, probability, stock)
        if not result["ok"]:
            reason = result["reason"]
            if reason == "duplicate":
                yield event.plain_result(
                    f"❌ 本群已存在同名奖品（编号：#{result['prize_id']}）。",
                )
            elif reason == "too_many":
                yield event.plain_result(
                    f"❌ 奖品数量已达上限（{MAX_LOTTERY_PRIZES} 个）。",
                )
            elif reason == "probability":
                yield event.plain_result(
                    "❌ 概率总和不能超过 100%\n"
                    f"已使用：{self._fmt_points(result['total'])}%，"
                    f"剩余可分配：{self._fmt_points(result['remaining'])}%",
                )
            else:
                yield event.plain_result("❌ 奖品参数不合法，请检查后重试。")
            return

        yield event.plain_result(
            "✅ 奖品添加成功\n"
            f"编号：#{result['prize_id']}\n"
            f"名称：{result['name']}\n"
            f"中奖概率：{self._fmt_points(result['probability'])}%\n"
            f"库存：{result['stock']}\n"
            f"当前概率合计：{self._fmt_points(result['probability_total'])}%",
        )

    @filter.command("删除奖品")
    async def delete_lottery_prize_command(self, event: AstrMessageEvent):
        """删除抽奖奖品，仅群主/管理员可用。"""
        if not self._claim_event(event):
            return
        async for result in self._run_safely(
            event,
            self._handle_delete_lottery_prize,
            action="删除奖品",
        ):
            yield result

    async def _handle_delete_lottery_prize(
        self,
        event: AstrMessageEvent,
    ) -> AsyncGenerator[Any, None]:
        """删除指定编号的奖品，历史抽奖记录保留。"""
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 该指令只能在群聊中使用。")
            return
        if not await self._is_group_admin(event):
            yield event.plain_result("❌ 该指令仅限群主/管理员使用。")
            return

        raw = self._command_remainder(event, "删除奖品")
        prize_id, error = self._resolve_item_reference(
            self.db.list_lottery_prizes(group_id),
            raw,
            "奖品",
        )
        if prize_id is None:
            yield event.plain_result(
                error or "❌ 奖品编号不合法，请使用：/删除奖品 奖品编号或名称",
            )
            return

        prize = self.db.get_lottery_prize(group_id, prize_id)
        if prize is None:
            yield event.plain_result(f"❌ 奖品 #{prize_id} 不存在。")
            return

        self.db.delete_lottery_prize(group_id, prize_id)
        yield event.plain_result(
            f"✅ 已删除奖品 #{prize_id}「{prize['name']}」\n历史抽奖记录仍然保留。",
        )

    @filter.command("抽奖记录")
    async def lottery_logs_command(self, event: AstrMessageEvent):
        """查看个人抽奖历史。"""
        if not self._claim_event(event):
            return
        async for result in self._run_safely(
            event,
            self._handle_lottery_logs,
            action="查询抽奖记录",
        ):
            yield result

    async def _handle_lottery_logs(
        self,
        event: AstrMessageEvent,
    ) -> AsyncGenerator[Any, None]:
        """展示当前用户最近 10 条抽奖记录。"""
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 抽奖记录只能在群聊中查看哦～")
            return

        user_id = str(event.get_sender_id() or "").strip()
        if not user_id:
            yield event.plain_result("❌ 无法获取你的用户 ID。")
            return

        self._touch_group_and_user(
            event,
            group_id,
            user_id,
            event.get_sender_name() or "",
        )

        rows = self.db.get_lottery_logs(group_id, user_id, 10)
        lines = ["🎰 我的抽奖记录"]
        if not rows:
            lines.append("暂无抽奖记录，发送 /抽奖 试试手气吧～")
        else:
            for row in rows:
                if int(row["is_win"] or 0):
                    tag = "🎉 中奖"
                    if int(row["guaranteed"] or 0):
                        tag += "（保底）"
                    lines.append(
                        f"{row['created_at']} {tag}「{row['prize_name']}」 "
                        f"消耗 {self._fmt_points(row['cost'])} 积分",
                    )
                else:
                    lines.append(
                        f"{row['created_at']} 😢 未中奖 "
                        f"消耗 {self._fmt_points(row['cost'])} 积分",
                    )

        yield event.plain_result("\n".join(lines))

    @filter.command("抽奖统计")
    async def lottery_stats_command(self, event: AstrMessageEvent):
        """查看本群抽奖统计，仅群主/管理员可用。"""
        if not self._claim_event(event):
            return
        async for result in self._run_safely(
            event,
            self._handle_lottery_stats,
            action="查看抽奖统计",
        ):
            yield result

    async def _handle_lottery_stats(
        self,
        event: AstrMessageEvent,
    ) -> AsyncGenerator[Any, None]:
        """展示抽奖次数、中奖率与各奖品中奖次数。"""
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 该指令只能在群聊中使用。")
            return
        if not await self._is_group_admin(event):
            yield event.plain_result("❌ 该指令仅限群主/管理员使用。")
            return

        stats = self.db.get_lottery_stats(group_id)
        lines = [
            "📈 本群抽奖统计",
            f"参与人数：{int(stats.get('user_count', 0) or 0)} 人",
            f"抽奖次数：{int(stats.get('total_draws', 0) or 0)} 次",
            f"中奖次数：{int(stats.get('total_wins', 0) or 0)} 次",
            f"中奖率：{self._fmt_points(stats.get('win_rate', 0))}%",
            f"累计消耗积分：{self._fmt_points(stats.get('total_cost', 0))}",
        ]
        prizes = stats.get("prizes") or []
        if prizes:
            lines.append("")
            lines.append("各奖品中奖次数：")
            for prize in prizes:
                lines.append(f"· {prize['prize_name']}：{int(prize['wins'])} 次")

        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------------------------
    # 每日任务系统（v3.0.0）
    # ------------------------------------------------------------------
    @filter.command("任务", alias={"今日任务"})
    async def tasks_command(self, event: AstrMessageEvent):
        """查看今日任务与完成进度。"""
        if not self._claim_event(event):
            return
        async for result in self._run_safely(
            event,
            self._handle_tasks,
            action="查看任务",
        ):
            yield result

    async def _handle_tasks(
        self,
        event: AstrMessageEvent,
    ) -> AsyncGenerator[Any, None]:
        """展示本群任务及当前用户的完成进度。"""
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 任务只能在群聊中查看哦～")
            return

        user_id = str(event.get_sender_id() or "").strip()
        if not user_id:
            yield event.plain_result("❌ 无法获取你的用户 ID。")
            return

        nickname = event.get_sender_name() or ""
        self._touch_group_and_user(event, group_id, user_id, nickname)

        # 先结算一次，确保展示的是最新进度
        reward_lines = self._settle_rewards(group_id, user_id, nickname)
        tasks = self.db.get_tasks_with_progress(group_id, user_id)

        lines = ["📝 今日任务"]
        if not tasks:
            lines.append("暂无任务，请联系管理员添加。")
        else:
            completed_count = sum(1 for task in tasks if task["completed"])
            lines.append(f"完成进度：{completed_count}/{len(tasks)}")
            for task in tasks:
                target = float(task["target_value"])
                progress = float(task["progress"])
                status = "✅ 已完成" if task["completed"] else (
                    f"{self._fmt_points(progress)}/{self._fmt_points(target)}"
                )
                lines.append(
                    f"[#{task['id']}] {task['name']} — {status}"
                    f"（+{task['reward_points']} 积分 +{task['reward_exp']} 经验）",
                )
        if reward_lines:
            lines.append("")
            lines.extend(reward_lines)

        yield event.plain_result("\n".join(lines))

    @filter.command("任务列表")
    async def task_list_command(self, event: AstrMessageEvent):
        """查看本群全部任务。"""
        if not self._claim_event(event):
            return
        async for result in self._run_safely(
            event,
            self._handle_task_list,
            action="查看任务列表",
        ):
            yield result

    async def _handle_task_list(
        self,
        event: AstrMessageEvent,
    ) -> AsyncGenerator[Any, None]:
        """展示任务类型、目标与奖励。"""
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 任务列表只能在群聊中查看哦～")
            return

        tasks = self.db.list_tasks(group_id)
        lines = ["📋 任务列表"]
        if not tasks:
            lines.append("暂无任务，管理员可使用 /添加任务 添加。")
        else:
            for task in tasks:
                label = TASK_TYPE_LABELS.get(
                    str(task["task_type"]),
                    str(task["task_type"]),
                )
                default_tag = "（默认）" if int(task["is_default"] or 0) else ""
                lines.append(
                    f"[#{task['id']}] {task['name']}{default_tag} | "
                    f"{label} {int(task['target_value'])} | "
                    f"+{task['reward_points']} 积分 +{task['reward_exp']} 经验",
                )

        yield event.plain_result("\n".join(lines))

    @filter.command("添加任务")
    async def add_task_command(self, event: AstrMessageEvent):
        """添加每日任务，仅群主/管理员可用。"""
        if not self._claim_event(event):
            return
        async for result in self._run_safely(
            event,
            self._handle_add_task,
            action="添加任务",
        ):
            yield result

    async def _handle_add_task(
        self,
        event: AstrMessageEvent,
    ) -> AsyncGenerator[Any, None]:
        """解析并添加任务。

        支持两种格式：
        - ``/添加任务 名称 类型 目标次数 积分奖励 经验奖励``
        - ``/添加任务 名称 类型 积分奖励 经验奖励``（目标次数使用默认值）
        """
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 该指令只能在群聊中使用。")
            return
        if not await self._is_group_admin(event):
            yield event.plain_result("❌ 该指令仅限群主/管理员使用。")
            return

        remainder = self._command_remainder(event, "添加任务").strip()
        parts = remainder.split()
        if len(parts) < 4:
            yield event.plain_result(
                "❌ 格式错误，请使用：\n"
                "/添加任务 名称 类型 积分奖励 经验奖励\n"
                "或：/添加任务 名称 类型 目标次数 积分奖励 经验奖励\n"
                "类型支持：签到 / 发言 / 连续签到 / 积分",
            )
            return

        task_type = ""
        name = ""
        target_raw: str | None = None
        points_raw = ""
        exp_raw = ""

        # 优先按 5 段解析：名称 类型 目标 积分 经验
        if len(parts) >= 5 and parts[-3].isdigit():
            task_type = parts[-4]
            target_raw = parts[-3]
            points_raw = parts[-2]
            exp_raw = parts[-1]
            name = " ".join(parts[:-4]).strip()
        else:
            task_type = parts[-3]
            points_raw = parts[-2]
            exp_raw = parts[-1]
            name = " ".join(parts[:-3]).strip()

        try:
            reward_points = int(points_raw)
            reward_exp = int(exp_raw)
            target_value = int(target_raw) if target_raw is not None else None
        except (TypeError, ValueError):
            yield event.plain_result("❌ 积分奖励与经验奖励必须是整数。")
            return

        if not name or len(name) > 30:
            yield event.plain_result("❌ 任务名称不能为空，且不能超过 30 个字符。")
            return
        if reward_points < 0 or reward_points > 1_000_000:
            yield event.plain_result("❌ 积分奖励必须是 0~1000000 之间的整数。")
            return
        if reward_exp < 0 or reward_exp > 1_000_000:
            yield event.plain_result("❌ 经验奖励必须是 0~1000000 之间的整数。")
            return
        if target_value is None:
            target_value = 0  # 由数据库按类型填充默认目标
        if target_value < 0 or target_value > 1_000_000:
            yield event.plain_result("❌ 目标次数必须是 0~1000000 之间的整数。")
            return

        result = self.db.add_task(
            group_id,
            name,
            task_type,
            target_value,
            reward_points,
            reward_exp,
        )
        if not result["ok"]:
            if result["reason"] == "duplicate":
                yield event.plain_result(
                    f"❌ 本群已存在同名任务（编号：#{result['task_id']}）。",
                )
            elif result["reason"] == "unknown_type":
                yield event.plain_result(
                    "❌ 未知任务类型，支持：签到 / 发言 / 连续签到 / 积分\n"
                    "也可使用英文：sign_in / chat / streak / points",
                )
            else:
                yield event.plain_result("❌ 任务参数不合法，请检查后重试。")
            return

        yield event.plain_result(
            "✅ 任务添加成功\n"
            f"编号：#{result['task_id']}\n"
            f"名称：{result['name']}\n"
            f"类型：{TASK_TYPE_LABELS.get(result['task_type'], result['task_type'])}\n"
            f"目标次数：{result['target_value']}\n"
            f"奖励：+{result['reward_points']} 积分 "
            f"+{result['reward_exp']} 经验",
        )

    @filter.command("删除任务")
    async def delete_task_command(self, event: AstrMessageEvent):
        """删除每日任务，仅群主/管理员可用。"""
        if not self._claim_event(event):
            return
        async for result in self._run_safely(
            event,
            self._handle_delete_task,
            action="删除任务",
        ):
            yield result

    async def _handle_delete_task(
        self,
        event: AstrMessageEvent,
    ) -> AsyncGenerator[Any, None]:
        """删除指定编号的任务，历史完成记录保留。"""
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 该指令只能在群聊中使用。")
            return
        if not await self._is_group_admin(event):
            yield event.plain_result("❌ 该指令仅限群主/管理员使用。")
            return

        raw = self._command_remainder(event, "删除任务")
        task_id, error = self._resolve_item_reference(
            self.db.list_tasks(group_id),
            raw,
            "任务",
        )
        if task_id is None:
            yield event.plain_result(
                error or "❌ 任务编号不合法，请使用：/删除任务 任务编号或名称",
            )
            return

        task = self.db.get_task(group_id, task_id)
        if task is None:
            yield event.plain_result(f"❌ 任务 #{task_id} 不存在。")
            return

        self.db.delete_task(group_id, task_id)
        yield event.plain_result(
            f"✅ 已删除任务 #{task_id}「{task['name']}」\n历史完成记录仍然保留。",
        )

    # ------------------------------------------------------------------
    # 成就系统（v3.0.0）
    # ------------------------------------------------------------------
    @filter.command("成就")
    async def achievements_command(self, event: AstrMessageEvent):
        """查看个人已达成成就。"""
        if not self._claim_event(event):
            return
        async for result in self._run_safely(
            event,
            self._handle_achievements,
            action="查看成就",
        ):
            yield result

    async def _handle_achievements(
        self,
        event: AstrMessageEvent,
    ) -> AsyncGenerator[Any, None]:
        """展示当前用户已达成的成就。"""
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 成就只能在群聊中查看哦～")
            return

        user_id = str(event.get_sender_id() or "").strip()
        if not user_id:
            yield event.plain_result("❌ 无法获取你的用户 ID。")
            return

        nickname = event.get_sender_name() or ""
        self._touch_group_and_user(event, group_id, user_id, nickname)

        # 先检测一次，避免历史数据未结算
        reward_lines = self._settle_rewards(group_id, user_id, nickname)
        rows = self.db.get_user_achievements(group_id, user_id)
        total = len(self.db.list_achievements(group_id))

        lines = [f"🏆 我的成就（{len(rows)}/{total}）"]
        if not rows:
            lines.append("暂无成就，继续签到、聊天、兑换吧～")
        else:
            for row in rows:
                lines.append(
                    f"· {row['name']} — {row['description']} "
                    f"（{row['achieved_at']}）",
                )
        if reward_lines:
            lines.append("")
            lines.extend(reward_lines)

        yield event.plain_result("\n".join(lines))

    @filter.command("成就列表")
    async def achievement_list_command(self, event: AstrMessageEvent):
        """查看全部成就及达成条件。"""
        if not self._claim_event(event):
            return
        async for result in self._run_safely(
            event,
            self._handle_achievement_list,
            action="查看成就列表",
        ):
            yield result

    async def _handle_achievement_list(
        self,
        event: AstrMessageEvent,
    ) -> AsyncGenerator[Any, None]:
        """展示所有成就、达成条件与奖励，并标记是否已达成。"""
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 成就列表只能在群聊中查看哦～")
            return

        user_id = str(event.get_sender_id() or "").strip()
        if not user_id:
            yield event.plain_result("❌ 无法获取你的用户 ID。")
            return

        self._touch_group_and_user(
            event,
            group_id,
            user_id,
            event.get_sender_name() or "",
        )

        rows = self.db.list_achievements(group_id, user_id)
        lines = ["🏆 成就列表"]
        if not rows:
            lines.append("暂无成就定义。")
        else:
            for row in rows:
                achieved = int(row.get("achieved") or 0) == 1
                tag = "✅" if achieved else "⬜"
                lines.append(
                    f"{tag} {row['name']} — {row['description']} "
                    f"（+{row['reward_points']} 积分 +{row['reward_exp']} 经验）",
                )

        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------------------------
    # 积分转账（v3.0.0）
    # ------------------------------------------------------------------
    @filter.command("转账")
    async def transfer_command(self, event: AstrMessageEvent, amount: str = ""):
        """转账积分给其他成员，例如：/转账 @张三 100。"""
        if not self._claim_event(event):
            return
        async for result in self._run_safely(
            event,
            self._handle_transfer,
            amount,
            action="积分转账",
        ):
            yield result

    async def _handle_transfer(
        self,
        event: AstrMessageEvent,
        raw_amount: str = "",
    ) -> AsyncGenerator[Any, None]:
        """解析转账对象与数量并执行转账。"""
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 转账只能在群聊中使用哦～")
            return

        user_id = str(event.get_sender_id() or "").strip()
        if not user_id:
            yield event.plain_result("❌ 无法获取你的用户 ID，转账失败。")
            return

        target_id, target_name = self._resolve_target(event, group_id)
        if not target_id:
            yield event.plain_result(
                "❌ 请使用 @ 指定收款人，例如：/转账 @张三 100",
            )
            return
        if target_id == user_id:
            yield event.plain_result("❌ 不能给自己转账哦～")
            return

        # 解析转账数量：优先使用框架参数，失败时从原始消息中查找
        parsed = self._parse_points_amount(raw_amount)
        if parsed is None:
            remainder = self._command_remainder(event, "转账")
            for token in remainder.split():
                if str(token).lstrip("@＠") == str(target_id):
                    continue
                candidate = self._parse_points_amount(token)
                if candidate is not None:
                    parsed = candidate
                    break
        if parsed is None:
            yield event.plain_result(
                "❌ 转账数量不合法，请输入大于 0 的数字，例如：/转账 @张三 100",
            )
            return

        nickname = event.get_sender_name() or ""
        self._touch_group_and_user(event, group_id, user_id, nickname)

        result = self.db.transfer_points(
            group_id,
            user_id,
            target_id,
            parsed,
            nickname,
            target_name,
        )
        status = result["status"]

        if status == "same_user":
            yield event.plain_result("❌ 不能给自己转账哦～")
            return
        if status == "invalid_amount":
            yield event.plain_result(
                f"❌ 单次转账数量必须在 0~{TRANSFER_DAILY_LIMIT} 之间。",
            )
            return
        if status == "too_young":
            yield event.plain_result(
                f"❌ 新成员注册满 {TRANSFER_MIN_AGE_DAYS} 天后才能转账，"
                f"还需 {result['days_left']} 天。",
            )
            return
        if status == "daily_limit":
            yield event.plain_result(
                "❌ 今日转账额度不足\n"
                f"今日已转出：{self._fmt_points(result['daily_used'])} 积分\n"
                f"今日剩余额度：{self._fmt_points(result['daily_remaining'])} 积分\n"
                f"每日限额：{TRANSFER_DAILY_LIMIT} 积分",
            )
            return
        if status == "insufficient":
            yield event.plain_result(
                "❌ 积分不足，无法完成转账\n"
                f"转账金额：{self._fmt_points(parsed)} 积分\n"
                f"手续费：{self._fmt_points(result['fee'])} 积分\n"
                f"合计需要：{self._fmt_points(result['need'])} 积分\n"
                f"当前积分：{self._fmt_points(result['points'])}",
            )
            return

        lines = [
            "💸 转账成功",
            f"转出：{nickname or user_id}",
            f"转入：{target_name}",
            f"转账金额：{self._fmt_points(result['amount'])} 积分",
            f"手续费：{self._fmt_points(result['fee'])} 积分"
            f"（{int(TRANSFER_FEE_RATE * 100)}%，最低 {TRANSFER_MIN_FEE} 积分）",
            f"合计扣除：{self._fmt_points(result['total'])} 积分",
            f"你的余额：{self._fmt_points(result['points'])} 积分",
            f"对方余额：{self._fmt_points(result['to_points'])} 积分",
            f"今日剩余额度：{self._fmt_points(result['daily_remaining'])} 积分",
        ]

        # 转账后检测双方成就（如“土豪”“积分大亨”）
        for achievement in self.db.check_achievements(group_id, user_id, nickname):
            lines.append(
                f"🏆 你达成成就「{achievement['name']}」："
                f"+{achievement['reward_points']} 积分 "
                f"+{achievement['reward_exp']} 经验",
            )
        for achievement in self.db.check_achievements(
            group_id,
            target_id,
            target_name,
        ):
            lines.append(
                f"🏆 {target_name} 达成成就「{achievement['name']}」",
            )

        yield event.plain_result("\n".join(lines))

    @filter.command("转账记录")
    async def transfer_logs_command(self, event: AstrMessageEvent):
        """查看个人转账历史。"""
        if not self._claim_event(event):
            return
        async for result in self._run_safely(
            event,
            self._handle_transfer_logs,
            action="查询转账记录",
        ):
            yield result

    async def _handle_transfer_logs(
        self,
        event: AstrMessageEvent,
    ) -> AsyncGenerator[Any, None]:
        """展示与当前用户相关的最近 10 条转账记录。"""
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 转账记录只能在群聊中查看哦～")
            return

        user_id = str(event.get_sender_id() or "").strip()
        if not user_id:
            yield event.plain_result("❌ 无法获取你的用户 ID。")
            return

        self._touch_group_and_user(
            event,
            group_id,
            user_id,
            event.get_sender_name() or "",
        )

        rows = self.db.get_transfer_logs(group_id, user_id, 10)
        lines = ["🧾 我的转账记录"]
        if not rows:
            lines.append("暂无转账记录，发送 /转账 @成员 数量 试试吧～")
        else:
            for row in rows:
                if str(row["from_user"]) == user_id:
                    peer = self.db.get_user(group_id, str(row["to_user"])) or {}
                    name = self._display_name(peer.get("nickname"), row["to_user"])
                    lines.append(
                        f"{row['created_at']} 转出给 {name} "
                        f"{self._fmt_points(row['amount'])} 积分"
                        f"（手续费 {self._fmt_points(row['fee'])}）",
                    )
                else:
                    peer = self.db.get_user(group_id, str(row["from_user"])) or {}
                    name = self._display_name(peer.get("nickname"), row["from_user"])
                    lines.append(
                        f"{row['created_at']} 收到 {name} "
                        f"{self._fmt_points(row['amount'])} 积分",
                    )

        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------------------------
    # 积分明细（v3.0.0）
    # ------------------------------------------------------------------
    @filter.command("积分明细")
    async def points_logs_command(self, event: AstrMessageEvent):
        """查看最近 20 条积分变动记录。"""
        if not self._claim_event(event):
            return
        async for result in self._run_safely(
            event,
            self._handle_points_logs,
            action="查询积分明细",
        ):
            yield result

    async def _handle_points_logs(
        self,
        event: AstrMessageEvent,
    ) -> AsyncGenerator[Any, None]:
        """展示时间、变动类型、变动数量与剩余积分。"""
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 积分明细只能在群聊中查看哦～")
            return

        user_id = str(event.get_sender_id() or "").strip()
        if not user_id:
            yield event.plain_result("❌ 无法获取你的用户 ID。")
            return

        self._touch_group_and_user(
            event,
            group_id,
            user_id,
            event.get_sender_name() or "",
        )

        type_icons = {
            "签到": "📅",
            "聊天": "💬",
            "兑换": "🛒",
            "转账": "💸",
            "任务": "📝",
            "成就": "🏆",
            "抽奖": "🎰",
            "管理": "🛠️",
        }

        rows = self.db.get_points_logs(group_id, user_id, 20)
        lines = ["📊 我的积分明细（最近 20 条）"]
        if not rows:
            lines.append("暂无积分变动记录，快去签到吧～")
        else:
            for row in rows:
                change = float(row["change"] or 0)
                change_text = (
                    f"+{self._fmt_points(change)}"
                    if change > 0
                    else f"-{self._fmt_points(abs(change))}"
                )
                icon = type_icons.get(str(row["log_type"]), "·")
                detail = f" {row['detail']}" if row["detail"] else ""
                lines.append(
                    f"{row['created_at']} {icon}{row['log_type']}"
                    f"{detail} {change_text} | 余额 "
                    f"{self._fmt_points(row['balance'])}",
                )

        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------------------------
    # v4.0.0 群管：违禁词
    # ------------------------------------------------------------------
    @filter.command("添加违禁词")
    async def add_banned_word_command(self, event: AstrMessageEvent):
        """添加违禁词，仅群主/管理员可用。"""
        if not self._claim_event(event):
            return
        async for result in self._run_safely(
            event,
            self._handle_add_banned_word,
            action="添加违禁词",
        ):
            yield result

    async def _handle_add_banned_word(
        self,
        event: AstrMessageEvent,
    ) -> AsyncGenerator[Any, None]:
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 该指令只能在群聊中使用。")
            return
        if not await self._is_group_admin(event):
            yield event.plain_result("❌ 该指令仅限群主/管理员使用。")
            return

        remainder = self._command_remainder(event, "添加违禁词")
        tokens = remainder.split()
        if not tokens:
            yield event.plain_result(
                "❌ 用法：/添加违禁词 词语 [惩罚类型] [时长]\n"
                "惩罚类型：警告 / 禁言 / 踢出 / 积分扣除\n"
                f"禁言默认 {DEFAULT_BANNED_DURATION} 分钟，"
                "积分扣除默认扣 10 分",
            )
            return

        word = tokens[0]
        punish_type = DEFAULT_BANNED_PUNISH
        duration = DEFAULT_BANNED_DURATION

        for token in tokens[1:]:
            if token.lower() in BANNED_PUNISH_ALIASES:
                punish_type = token
            elif token.isdigit():
                duration = int(token)

        result = self.db.add_banned_word(
            group_id,
            word,
            added_by=str(event.get_sender_id() or ""),
            punish_type=punish_type,
            punish_duration=duration,
        )
        if not result.get("ok"):
            yield event.plain_result(f"❌ {result.get('reason', '添加失败')}")
            return

        self._invalidate_banned_cache(group_id)
        duration_text = ""
        if result["punish_type"] == "禁言":
            duration_text = f"，禁言 {result['punish_duration']} 分钟"
        elif result["punish_type"] == "积分扣除":
            duration_text = f"，扣除 {result['punish_duration']} 积分"

        yield event.plain_result(
            f"✅ 已添加违禁词「{result['word']}」\n"
            f"惩罚方式：{result['punish_type']}{duration_text}",
        )

    @filter.command("删除违禁词")
    async def delete_banned_word_command(self, event: AstrMessageEvent):
        """删除违禁词，仅群主/管理员可用。"""
        if not self._claim_event(event):
            return
        async for result in self._run_safely(
            event,
            self._handle_delete_banned_word,
            action="删除违禁词",
        ):
            yield result

    async def _handle_delete_banned_word(
        self,
        event: AstrMessageEvent,
    ) -> AsyncGenerator[Any, None]:
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 该指令只能在群聊中使用。")
            return
        if not await self._is_group_admin(event):
            yield event.plain_result("❌ 该指令仅限群主/管理员使用。")
            return

        word = self._command_remainder(event, "删除违禁词").strip()
        if not word:
            yield event.plain_result("❌ 用法：/删除违禁词 词语")
            return

        if self.db.delete_banned_word(group_id, word):
            self._invalidate_banned_cache(group_id)
            yield event.plain_result(f"✅ 已删除违禁词「{word}」。")
        else:
            yield event.plain_result(f"❌ 没有找到违禁词「{word}」。")

    @filter.command("违禁词列表")
    async def banned_word_list_command(self, event: AstrMessageEvent):
        """查看本群违禁词列表。"""
        if not self._claim_event(event):
            return
        async for result in self._run_safely(
            event,
            self._handle_banned_word_list,
            action="查询违禁词",
        ):
            yield result

    async def _handle_banned_word_list(
        self,
        event: AstrMessageEvent,
    ) -> AsyncGenerator[Any, None]:
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 该指令只能在群聊中使用。")
            return

        words = self.db.list_banned_words(group_id)
        if not words:
            yield event.plain_result("📋 本群还没有设置违禁词。")
            return

        lines = [f"📋 本群违禁词（{len(words)} 个）"]
        for item in words:
            punish = str(item["punish_type"])
            if punish == "禁言":
                punish += f" {item['punish_duration']} 分钟"
            elif punish == "积分扣除":
                punish += f" {item['punish_duration']} 分"
            lines.append(f"#{item['id']}「{item['word']}」→ {punish}")
        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------------------------
    # v4.0.0 群管：自动审核开关与配置
    # ------------------------------------------------------------------
    @filter.command("开启防刷屏")
    async def enable_anti_spam_command(self, event: AstrMessageEvent):
        """开启防刷屏。"""
        if not self._claim_event(event):
            return
        async for result in self._run_safely(
            event,
            self._handle_toggle_feature,
            "anti_spam",
            True,
            action="开启防刷屏",
        ):
            yield result

    @filter.command("关闭防刷屏")
    async def disable_anti_spam_command(self, event: AstrMessageEvent):
        """关闭防刷屏。"""
        if not self._claim_event(event):
            return
        async for result in self._run_safely(
            event,
            self._handle_toggle_feature,
            "anti_spam",
            False,
            action="关闭防刷屏",
        ):
            yield result

    @filter.command("开启防广告")
    async def enable_anti_ad_command(self, event: AstrMessageEvent):
        """开启防广告。"""
        if not self._claim_event(event):
            return
        async for result in self._run_safely(
            event,
            self._handle_toggle_feature,
            "anti_ad",
            True,
            action="开启防广告",
        ):
            yield result

    @filter.command("关闭防广告")
    async def disable_anti_ad_command(self, event: AstrMessageEvent):
        """关闭防广告。"""
        if not self._claim_event(event):
            return
        async for result in self._run_safely(
            event,
            self._handle_toggle_feature,
            "anti_ad",
            False,
            action="关闭防广告",
        ):
            yield result

    @filter.command("开启夜间模式")
    async def enable_night_mode_command(self, event: AstrMessageEvent):
        """开启夜间模式。"""
        if not self._claim_event(event):
            return
        async for result in self._run_safely(
            event,
            self._handle_toggle_feature,
            "night_mode",
            True,
            action="开启夜间模式",
        ):
            yield result

    @filter.command("关闭夜间模式")
    async def disable_night_mode_command(self, event: AstrMessageEvent):
        """关闭夜间模式。"""
        if not self._claim_event(event):
            return
        async for result in self._run_safely(
            event,
            self._handle_toggle_feature,
            "night_mode",
            False,
            action="关闭夜间模式",
        ):
            yield result

    async def _handle_toggle_feature(
        self,
        event: AstrMessageEvent,
        feature: str,
        enabled: bool,
    ) -> AsyncGenerator[Any, None]:
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 该指令只能在群聊中使用。")
            return
        if not await self._is_group_admin(event):
            yield event.plain_result("❌ 该指令仅限群主/管理员使用。")
            return

        settings = self.db.set_auto_mod_feature(group_id, feature, enabled=enabled)
        self._invalidate_mod_cache(group_id)
        label = AUTO_MOD_LABELS.get(feature, feature)
        status = "已开启" if settings.get("enabled") else "已关闭"

        extra = ""
        if feature == "anti_spam" and settings.get("enabled"):
            extra = (
                f"\n阈值：{settings['threshold']} 条 / "
                f"{settings['window']} 秒，触发禁言 "
                f"{settings['mute_minutes']} 分钟"
            )
        elif feature == "anti_ad" and settings.get("enabled"):
            extra = f"\n触发后禁言 {settings['mute_minutes']} 分钟"
        elif feature == "night_mode" and settings.get("enabled"):
            extra = (
                f"\n夜间 {NIGHT_START_HOUR}:00 - {NIGHT_END_HOUR}:00 "
                f"自动禁言 {settings['mute_minutes']} 分钟"
            )

        yield event.plain_result(f"✅ 本群{label}{status}。{extra}")

    @filter.command("设置刷屏阈值")
    async def set_spam_threshold_command(self, event: AstrMessageEvent):
        """设置防刷屏阈值，仅群主/管理员可用。"""
        if not self._claim_event(event):
            return
        async for result in self._run_safely(
            event,
            self._handle_set_spam_threshold,
            action="设置刷屏阈值",
        ):
            yield result

    async def _handle_set_spam_threshold(
        self,
        event: AstrMessageEvent,
    ) -> AsyncGenerator[Any, None]:
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 该指令只能在群聊中使用。")
            return
        if not await self._is_group_admin(event):
            yield event.plain_result("❌ 该指令仅限群主/管理员使用。")
            return

        tokens = self._command_remainder(event, "设置刷屏阈值").split()
        if len(tokens) < 2 or not all(token.isdigit() for token in tokens[:2]):
            yield event.plain_result(
                "❌ 用法：/设置刷屏阈值 次数 秒数\n"
                f"默认：{DEFAULT_SPAM_THRESHOLD} 条 / "
                f"{DEFAULT_SPAM_WINDOW} 秒",
            )
            return

        threshold, window = int(tokens[0]), int(tokens[1])
        if not 2 <= threshold <= 50:
            yield event.plain_result("❌ 次数需在 2 ~ 50 之间。")
            return
        if not 1 <= window <= 300:
            yield event.plain_result("❌ 秒数需在 1 ~ 300 之间。")
            return

        settings = self.db.set_auto_mod_feature(
            group_id,
            "anti_spam",
            config={"threshold": threshold, "window": window},
        )
        self._invalidate_mod_cache(group_id)
        yield event.plain_result(
            f"✅ 防刷屏阈值已设置为 {settings['threshold']} 条 / "
            f"{settings['window']} 秒。",
        )

    @filter.command("设置广告惩罚")
    async def set_ad_punish_command(self, event: AstrMessageEvent):
        """设置防广告禁言时长，仅群主/管理员可用。"""
        if not self._claim_event(event):
            return
        async for result in self._run_safely(
            event,
            self._handle_set_ad_punish,
            action="设置广告惩罚",
        ):
            yield result

    async def _handle_set_ad_punish(
        self,
        event: AstrMessageEvent,
    ) -> AsyncGenerator[Any, None]:
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 该指令只能在群聊中使用。")
            return
        if not await self._is_group_admin(event):
            yield event.plain_result("❌ 该指令仅限群主/管理员使用。")
            return

        raw = self._command_remainder(event, "设置广告惩罚").strip()
        if not raw.isdigit():
            yield event.plain_result(
                "❌ 用法：/设置广告惩罚 禁言时长（分钟）\n"
                f"默认：{DEFAULT_AD_MUTE_MINUTES} 分钟",
            )
            return

        minutes = int(raw)
        if not 1 <= minutes <= MAX_MUTE_MINUTES:
            yield event.plain_result(
                f"❌ 禁言时长需在 1 ~ {MAX_MUTE_MINUTES} 分钟之间。",
            )
            return

        settings = self.db.set_auto_mod_feature(
            group_id,
            "anti_ad",
            config={"mute_minutes": minutes},
        )
        self._invalidate_mod_cache(group_id)
        yield event.plain_result(
            f"✅ 防广告触发后的禁言时长已设置为 "
            f"{settings['mute_minutes']} 分钟。",
        )

    # ------------------------------------------------------------------
    # v4.0.0 群管：成员管理
    # ------------------------------------------------------------------
    @filter.command("禁言")
    async def mute_member_command(self, event: AstrMessageEvent):
        """手动禁言成员，仅群主/管理员可用。"""
        if not self._claim_event(event):
            return
        async for result in self._run_safely(
            event,
            self._handle_mute_member,
            action="禁言成员",
        ):
            yield result

    async def _handle_mute_member(
        self,
        event: AstrMessageEvent,
    ) -> AsyncGenerator[Any, None]:
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 该指令只能在群聊中使用。")
            return
        if not await self._is_group_admin(event):
            yield event.plain_result("❌ 该指令仅限群主/管理员使用。")
            return

        target_id, target_name = self._resolve_target(event, group_id)
        if not target_id:
            yield event.plain_result("❌ 用法：/禁言 @某人 时长（分钟）")
            return

        config = self._reload_config().get("punishment", {})
        tokens = self._command_remainder(event, "禁言").split()
        minutes = int(config.get("mute_duration", DEFAULT_BANNED_DURATION) or 10)
        for token in tokens:
            cleaned = self._clean_target_token(token)
            # 跳过目标用户 ID，避免把 /禁言 900010 3 中的 ID 当时长
            if cleaned and cleaned == str(target_id):
                continue
            if cleaned.isdigit():
                minutes = int(cleaned)
                break
        if not 1 <= minutes <= MAX_MUTE_MINUTES:
            yield event.plain_result(
                f"❌ 禁言时长需在 1 ~ {MAX_MUTE_MINUTES} 分钟之间。",
            )
            return

        platform = PlatformAdapter.detect(event)
        executor = str(event.get_sender_id() or "")
        if not PlatformAdapter.supports_mute(platform):
            applied, text, _ = self._apply_mute_fallback(
                group_id,
                target_id,
                target_name,
                "管理员手动禁言",
                executor,
                PlatformAdapter.label(platform),
            )
            yield event.plain_result(text)
            if not applied:
                self.db.add_punish_log(
                    group_id,
                    target_id,
                    "禁言",
                    f"管理员手动禁言（平台不支持：{PlatformAdapter.label(platform)}）",
                    duration=minutes,
                    executor_id=executor,
                )
            return

        ok, message = await PlatformAdapter.mute(event, target_id, minutes)
        if not ok:
            yield event.plain_result(
                f"❌ {message}\n"
                "💡 可改用 /警告 @成员 原因 或 /积分惩罚 @成员 数量，"
                "也可由管理员手动禁言。",
            )
            return

        self.db.add_punish_log(
            group_id,
            target_id,
            "禁言",
            "管理员手动禁言",
            duration=minutes,
            executor_id=executor,
        )
        yield event.plain_result(
            f"✅ 已禁言 {target_name} {minutes} 分钟。",
        )

    @filter.command("解除禁言")
    async def unmute_member_command(self, event: AstrMessageEvent):
        """解除成员禁言，仅群主/管理员可用。"""
        if not self._claim_event(event):
            return
        async for result in self._run_safely(
            event,
            self._handle_unmute_member,
            action="解除禁言",
        ):
            yield result

    async def _handle_unmute_member(
        self,
        event: AstrMessageEvent,
    ) -> AsyncGenerator[Any, None]:
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 该指令只能在群聊中使用。")
            return
        if not await self._is_group_admin(event):
            yield event.plain_result("❌ 该指令仅限群主/管理员使用。")
            return

        target_id, target_name = self._resolve_target(event, group_id)
        if not target_id:
            yield event.plain_result("❌ 用法：/解除禁言 @某人")
            return

        platform = PlatformAdapter.detect(event)
        if not PlatformAdapter.supports_unmute(platform):
            yield event.plain_result(
                f"⚠️ 当前平台不支持直接解除禁言"
                f"（{PlatformAdapter.label(platform)}）。\n"
                "请管理员在平台后台手动解除。",
            )
            return

        ok, message = await PlatformAdapter.unmute(event, target_id)
        if not ok:
            yield event.plain_result(f"❌ {message}")
            return

        self.db.add_punish_log(
            group_id,
            target_id,
            "解除禁言",
            "管理员解除禁言",
            executor_id=str(event.get_sender_id() or ""),
        )
        yield event.plain_result(f"✅ 已解除 {target_name} 的禁言。")

    @filter.command("踢出")
    async def kick_member_command(self, event: AstrMessageEvent):
        """踢出成员，仅群主/管理员可用。"""
        if not self._claim_event(event):
            return
        async for result in self._run_safely(
            event,
            self._handle_kick_member,
            action="踢出成员",
        ):
            yield result

    async def _handle_kick_member(
        self,
        event: AstrMessageEvent,
    ) -> AsyncGenerator[Any, None]:
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 该指令只能在群聊中使用。")
            return
        if not await self._is_group_admin(event):
            yield event.plain_result("❌ 该指令仅限群主/管理员使用。")
            return

        target_id, target_name = self._resolve_target(event, group_id)
        if not target_id:
            yield event.plain_result("❌ 用法：/踢出 @某人")
            return

        platform = PlatformAdapter.detect(event)
        if not PlatformAdapter.supports_kick(platform):
            yield event.plain_result(
                f"⚠️ 当前平台不支持直接踢出"
                f"（{PlatformAdapter.label(platform)}）。\n"
                "请管理员在平台后台手动处理。",
            )
            return

        ok, error = await self._onebot_action(
            event,
            "set_group_kick",
            group_id=group_id,
            user_id=target_id,
            reject_add_request=False,
        )
        if not ok:
            yield event.plain_result(f"❌ 踢出失败：{error}")
            return

        self.db.add_punish_log(
            group_id,
            target_id,
            "踢出",
            "管理员手动踢出",
            executor_id=str(event.get_sender_id() or ""),
        )
        yield event.plain_result(f"✅ 已将 {target_name} 移出本群。")

    @filter.command("警告")
    async def warn_member_command(self, event: AstrMessageEvent):
        """警告成员，仅群主/管理员可用。"""
        if not self._claim_event(event):
            return
        async for result in self._run_safely(
            event,
            self._handle_warn_member,
            action="警告成员",
        ):
            yield result

    async def _handle_warn_member(
        self,
        event: AstrMessageEvent,
    ) -> AsyncGenerator[Any, None]:
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 该指令只能在群聊中使用。")
            return
        if not await self._is_group_admin(event):
            yield event.plain_result("❌ 该指令仅限群主/管理员使用。")
            return

        target_id, target_name = self._resolve_target(event, group_id)
        if not target_id:
            yield event.plain_result("❌ 用法：/警告 @某人 原因")
            return

        reason = self._extract_reason(event, "警告", target_name, target_id)
        result = self.db.add_warning(
            group_id,
            target_id,
            reason,
            warned_by=str(event.get_sender_id() or ""),
        )
        yield event.plain_result(
            f"⚠️ 已警告 {target_name}：{result['reason']}",
        )

    # ------------------------------------------------------------------
    # v1.5.3 平台适配与通用惩罚
    # ------------------------------------------------------------------
    def _apply_mute_fallback(
        self,
        group_id: str,
        user_id: str,
        nickname: str,
        reason: str,
        executor: str,
        platform_label: str,
    ) -> tuple[bool, str, int]:
        """平台不支持禁言时的降级处理：记录警告 + 扣除积分。

        Returns:
            ``(是否已降级, 提示文本, 当前警告次数)``
        """
        config = self._reload_config().get("punishment", {})
        if not config.get("fallback_to_warning", True):
            return (
                False,
                f"⚠️ 当前平台不支持直接禁言（{platform_label}）。\n"
                "当前配置未开启自动降级，请管理员手动处理。",
                0,
            )

        self.db.add_warning(group_id, user_id, reason, warned_by=executor)
        deduction = int(config.get("points_deduction", 0) or 0)
        applied = 0
        if deduction > 0:
            result = self._deduct_points(
                group_id,
                user_id,
                nickname,
                deduction,
                reason=f"禁言降级扣分（{reason}）",
                executor=executor,
            )
            applied = int(result.get("applied", 0) or 0)

        count = len(self.db.get_warnings(group_id, user_id, 100))
        threshold = int(config.get("warning_threshold", 3) or 3)
        lines = [
            f"⚠️ 当前平台不支持直接禁言（{platform_label}）",
            "",
            "已自动降级为以下惩罚：",
            "- 记录警告 1 次",
        ]
        if deduction <= 0:
            lines.append("- 扣除积分：未启用")
        elif applied > 0:
            lines.append(f"- 扣除积分 {applied} 分")
        else:
            lines.append("- 扣除积分：该成员积分不足，未实际扣除")

        if count >= threshold:
            lines.extend(
                [
                    "",
                    f"🚨 该成员累计警告 {count} 次（阈值 {threshold}），"
                    "请管理员手动处理。",
                ],
            )
        else:
            lines.extend(["", "管理员可手动在平台内禁言该成员。"])
        return True, "\n".join(lines), count

    def _deduct_points(
        self,
        group_id: str,
        user_id: str,
        nickname: str,
        amount: int,
        reason: str,
        executor: str,
    ) -> dict[str, Any]:
        """扣除积分并写入惩罚记录，返回实际扣除与余额。"""
        result = self.db.adjust_points(
            group_id,
            user_id,
            -abs(int(amount)),
            nickname,
            reason=f"积分惩罚：{reason}",
        )
        applied = int(abs(result.get("applied", 0) or 0))
        self.db.add_punish_log(
            group_id,
            user_id,
            "积分扣除",
            reason,
            duration=applied,
            executor_id=executor,
        )
        return {
            "applied": applied,
            "balance": result.get("new", 0),
        }

    @filter.command("平台信息")
    async def platform_info_command(self, event: AstrMessageEvent):
        """查看当前平台支持的群管能力。"""
        if not self._claim_event(event):
            return
        async for result in self._run_safely(
            event,
            self._handle_platform_info,
            action="查看平台信息",
        ):
            yield result

    async def _handle_platform_info(
        self,
        event: AstrMessageEvent,
    ) -> AsyncGenerator[Any, None]:
        """展示平台、能力与降级策略，方便管理员判断可用操作。"""
        platform = PlatformAdapter.detect(event)
        caps = PlatformAdapter.capabilities(platform)
        config = self._reload_config().get("punishment", {})

        def mark(value: bool) -> str:
            return "✅ 支持" if value else "❌ 不支持"

        lines = [
            "🌐 平台信息",
            f"平台：{PlatformAdapter.label(platform)}（{platform}）",
            "",
            "【群管能力】",
            f"禁言：{mark(caps['mute'])}",
            f"解除禁言：{mark(caps['unmute'])}",
            f"踢出：{mark(caps['kick'])}",
            f"撤回消息：{mark(caps['recall'])}",
            "警告 / 扣积分：✅ 支持",
            "",
            "【惩罚配置】",
            f"自动禁言：{'开启' if config.get('auto_mute', True) else '关闭'}",
            f"默认禁言时长：{config.get('mute_duration', 10)} 分钟",
            f"警告阈值：{config.get('warning_threshold', 3)} 次",
            f"违规扣分：{config.get('points_deduction', 5)} 分",
            "禁言不可用时："
            + (
                "记录警告 + 扣积分"
                if config.get("fallback_to_warning", True)
                else "仅提示管理员手动处理"
            ),
        ]
        if not caps["mute"]:
            lines.extend(
                [
                    "",
                    "💡 当前平台无法直接禁言，发送 /禁言 会自动降级为警告 + 扣分。",
                ],
            )
        yield event.plain_result("\n".join(lines))

    @filter.command("惩罚记录")
    async def punish_history_command(self, event: AstrMessageEvent):
        """查看自己或指定成员的警告与惩罚历史。"""
        if not self._claim_event(event):
            return
        async for result in self._run_safely(
            event,
            self._handle_punish_history,
            action="查询惩罚记录",
        ):
            yield result

    def _punish_log_line(self, item: dict[str, Any]) -> str:
        """格式化一条惩罚记录。"""
        punish_type = str(item.get("punish_type") or "未知")
        duration = int(item.get("duration") or 0)
        detail = ""
        if duration and punish_type == "禁言":
            detail = f" {duration} 分钟"
        elif duration and punish_type == "积分扣除":
            detail = f" {duration} 积分"
        reason = str(item.get("reason") or "无原因")
        executor = str(item.get("executor_id") or "")
        executor_text = "系统" if executor in ("", "系统") else f"执行人 {executor}"
        return (
            f"{item.get('created_at', '')} {punish_type}{detail} · "
            f"{reason}（{executor_text}）"
        )

    async def _handle_punish_history(
        self,
        event: AstrMessageEvent,
    ) -> AsyncGenerator[Any, None]:
        """查看惩罚历史：管理员可查他人，普通成员只能查自己。"""
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 该指令只能在群聊中使用。")
            return

        sender_id = str(event.get_sender_id() or "").strip()
        target_id, target_name = self._resolve_target(event, group_id)
        if not target_id:
            target_id = sender_id
            user = self.db.get_user(group_id, target_id)
            target_name = self._display_name(
                (user or {}).get("nickname"),
                target_id,
            )
        if target_id != sender_id and not await self._is_group_admin(event):
            yield event.plain_result("❌ 只能查看自己的惩罚记录，管理员可查看他人。")
            return

        punishments = self.db.get_punish_logs(group_id, target_id, 20)
        warnings = self.db.get_warnings(group_id, target_id, 100)
        lines = [
            f"📜 惩罚记录：{target_name}",
            f"警告 {len(warnings)} 次 · 惩罚记录 {len(punishments)} 条",
            "",
        ]
        if not punishments:
            lines.append("暂无惩罚记录。")
        else:
            for index, item in enumerate(punishments, start=1):
                lines.append(f"{index}. {self._punish_log_line(item)}")

        threshold = int(
            self._reload_config().get("punishment", {}).get(
                "warning_threshold",
                3,
            )
            or 3,
        )
        if len(warnings) >= threshold:
            lines.extend(
                ["", f"🚨 累计警告已达阈值（{threshold} 次），建议管理员手动处理。"],
            )
        yield event.plain_result("\n".join(lines))

    @filter.command("手动惩罚")
    async def manual_punish_command(self, event: AstrMessageEvent):
        """手动执行警告、扣分或黑名单标记，仅群主/管理员可用。"""
        if not self._claim_event(event):
            return
        async for result in self._run_safely(
            event,
            self._handle_manual_punish,
            action="手动惩罚",
        ):
            yield result

    def _parse_punish_tokens(
        self,
        event: AstrMessageEvent,
        command: str,
        target_name: str,
        target_id: str = "",
    ) -> list[str]:
        """去掉 @ 与目标昵称/ID 后的指令参数。"""
        tokens: list[str] = []
        for token in self._command_remainder(event, command).split():
            if token.startswith("@") or token.startswith("[CQ:at"):
                continue
            if target_id and self._clean_target_token(token) == str(target_id):
                continue
            tokens.append(token)
        if tokens and target_name and tokens[0] == target_name:
            tokens.pop(0)
        return tokens

    async def _handle_manual_punish(
        self,
        event: AstrMessageEvent,
    ) -> AsyncGenerator[Any, None]:
        """手动惩罚：警告 / 扣分 / 黑名单。"""
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 该指令只能在群聊中使用。")
            return
        if not await self._is_group_admin(event):
            yield event.plain_result("❌ 该指令仅限群主/管理员使用。")
            return

        target_id, target_name = self._resolve_target(event, group_id)
        if not target_id:
            yield event.plain_result(
                "❌ 用法：/手动惩罚 @成员 警告|扣分|黑名单 [原因/数量]",
            )
            return

        tokens = self._parse_punish_tokens(
            event,
            "手动惩罚",
            target_name,
            target_id,
        )
        if not tokens:
            yield event.plain_result(
                "❌ 用法：/手动惩罚 @成员 警告|扣分|黑名单 [原因/数量]",
            )
            return

        raw_type = tokens[0].lower()
        rest = tokens[1:]
        executor = str(event.get_sender_id() or "")
        if raw_type in ("警告", "warn", "warning", "警告一次"):
            reason = " ".join(rest).strip() or "管理员手动警告"
            result = self.db.add_warning(
                group_id,
                target_id,
                reason,
                warned_by=executor,
            )
            count = len(self.db.get_warnings(group_id, target_id, 100))
            threshold = int(
                self._reload_config().get("punishment", {}).get(
                    "warning_threshold",
                    3,
                )
                or 3,
            )
            extra = (
                f"\n🚨 累计警告 {count} 次，已达阈值 {threshold}，请管理员手动处理。"
                if count >= threshold
                else ""
            )
            yield event.plain_result(
                f"⚠️ 已警告 {target_name}：{result['reason']}{extra}",
            )
            return

        if raw_type in ("扣分", "扣积分", "积分", "积分惩罚", "deduct"):
            amount = 0
            reason_parts: list[str] = []
            for token in rest:
                if not amount and token.isdigit():
                    amount = int(token)
                else:
                    reason_parts.append(token)
            if amount <= 0:
                amount = int(
                    self._reload_config().get("punishment", {}).get(
                        "points_deduction",
                        5,
                    )
                    or 5,
                )
            reason = " ".join(reason_parts).strip() or "管理员手动扣分"
            if amount > 100000:
                yield event.plain_result("❌ 单次扣除积分不能超过 100000。")
                return
            result = self._deduct_points(
                group_id,
                target_id,
                target_name,
                amount,
                reason,
                executor,
            )
            applied = int(result.get("applied", 0) or 0)
            if applied <= 0:
                yield event.plain_result(
                    f"⚠️ {target_name} 积分不足，未实际扣除。",
                )
            else:
                yield event.plain_result(
                    f"⚠️ 已扣除 {target_name} {applied} 积分。\n"
                    f"原因：{reason}\n"
                    f"当前积分：{self._fmt_points(result.get('balance', 0))}",
                )
            return

        if raw_type in ("黑名单", "拉黑", "blacklist"):
            reason = " ".join(rest).strip() or "管理员手动加入黑名单"
            self.db.add_punish_log(
                group_id,
                target_id,
                "黑名单",
                reason,
                executor_id=executor,
            )
            yield event.plain_result(
                f"🚫 已将 {target_name} 标记为黑名单。\n"
                f"原因：{reason}\n"
                "提示：该标记记录在惩罚历史中，可使用 /惩罚记录 查看；"
                "解除请使用 /清空违规（仅群主）。",
            )
            return

        yield event.plain_result(
            "❌ 未知惩罚类型。\n"
            "用法：/手动惩罚 @成员 警告|扣分|黑名单 [原因/数量]",
        )

    @filter.command("积分惩罚")
    async def points_punish_command(self, event: AstrMessageEvent):
        """扣除成员积分，仅群主/管理员可用。"""
        if not self._claim_event(event):
            return
        async for result in self._run_safely(
            event,
            self._handle_points_punish,
            action="积分惩罚",
        ):
            yield result

    async def _handle_points_punish(
        self,
        event: AstrMessageEvent,
    ) -> AsyncGenerator[Any, None]:
        """扣除指定成员积分，并写入惩罚记录。"""
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 该指令只能在群聊中使用。")
            return
        if not await self._is_group_admin(event):
            yield event.plain_result("❌ 该指令仅限群主/管理员使用。")
            return

        target_id, target_name = self._resolve_target(event, group_id)
        if not target_id:
            yield event.plain_result("❌ 用法：/积分惩罚 @某人 数量 [原因]")
            return

        tokens = self._parse_punish_tokens(
            event,
            "积分惩罚",
            target_name,
            target_id,
        )
        amount = 0
        reason_parts: list[str] = []
        for token in tokens:
            if not amount and token.isdigit():
                amount = int(token)
            else:
                reason_parts.append(token)
        if amount <= 0:
            yield event.plain_result("❌ 用法：/积分惩罚 @某人 数量 [原因]")
            return
        if amount > 100000:
            yield event.plain_result("❌ 单次扣除积分不能超过 100000。")
            return

        reason = " ".join(reason_parts).strip() or "管理员手动扣分"
        result = self._deduct_points(
            group_id,
            target_id,
            target_name,
            amount,
            reason,
            str(event.get_sender_id() or ""),
        )
        applied = int(result.get("applied", 0) or 0)
        if applied <= 0:
            yield event.plain_result(
                f"⚠️ {target_name} 积分不足，未实际扣除。",
            )
            return
        yield event.plain_result(
            f"⚠️ 已扣除 {target_name} {applied} 积分。\n"
            f"原因：{reason}\n"
            f"当前积分：{self._fmt_points(result.get('balance', 0))}",
        )

    @filter.command("成员信息")
    async def member_info_command(self, event: AstrMessageEvent):
        """查看成员完整信息，仅群主/管理员可用。"""
        if not self._claim_event(event):
            return
        async for result in self._run_safely(
            event,
            self._handle_member_info,
            action="查询成员信息",
        ):
            yield result

    async def _handle_member_info(
        self,
        event: AstrMessageEvent,
    ) -> AsyncGenerator[Any, None]:
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 该指令只能在群聊中使用。")
            return
        if not await self._is_group_admin(event):
            yield event.plain_result("❌ 该指令仅限群主/管理员使用。")
            return

        target_id, target_name = self._resolve_target(event, group_id)
        if not target_id:
            yield event.plain_result("❌ 用法：/成员信息 @某人")
            return

        user = self.db.get_user(group_id, target_id)
        if user is None:
            yield event.plain_result(
                f"❌ 暂无 {target_name} 的数据（该成员还未使用过签到/聊天等功能）。",
            )
            return

        punishments = self.db.get_punish_logs(group_id, target_id, 50)
        warnings = self.db.get_warnings(group_id, target_id, 50)
        whitelisted = self.db.is_whitelisted(group_id, target_id)
        name = self._display_name(user.get("nickname"), target_id)

        lines = [
            f"👤 成员信息：{name}（{target_id}）",
            f"积分：{self._fmt_points(user['points'])}",
            f"经验：{self._fmt_points(user['exp'])}",
            f"等级：Lv.{user['level']} {user['title']}",
            f"签到：累计 {user['total_signs']} 次，连续 {user['streak']} 天",
            f"违规：{len(punishments)} 次（其中警告 {len(warnings)} 次）",
            f"白名单：{'是' if whitelisted else '否'}",
        ]
        if punishments:
            latest = punishments[0]
            lines.append(
                f"最近处罚：{latest['punish_type']} - {latest['reason'] or '无原因'}"
                f"（{latest['created_at']}）",
            )
        yield event.plain_result("\n".join(lines))

    @filter.command("违规记录")
    async def violation_logs_command(self, event: AstrMessageEvent):
        """查看成员违规历史，仅群主/管理员可用。"""
        if not self._claim_event(event):
            return
        async for result in self._run_safely(
            event,
            self._handle_violation_logs,
            action="查询违规记录",
        ):
            yield result

    async def _handle_violation_logs(
        self,
        event: AstrMessageEvent,
    ) -> AsyncGenerator[Any, None]:
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 该指令只能在群聊中使用。")
            return
        if not await self._is_group_admin(event):
            yield event.plain_result("❌ 该指令仅限群主/管理员使用。")
            return

        target_id, target_name = self._resolve_target(event, group_id)
        if not target_id:
            yield event.plain_result("❌ 用法：/违规记录 @某人")
            return

        logs = self.db.get_punish_logs(group_id, target_id, 10)
        if not logs:
            yield event.plain_result(f"✅ {target_name} 暂无违规记录。")
            return

        lines = [f"📝 {target_name} 的最近违规记录（{len(logs)} 条）"]
        for item in logs:
            duration = ""
            if item["punish_type"] == "禁言" and item["duration"]:
                duration = f" {item['duration']} 分钟"
            elif item["punish_type"] == "积分扣除" and item["duration"]:
                duration = f" {item['duration']} 分"
            lines.append(
                f"{item['created_at']} · {item['punish_type']}{duration}"
                f" · {item['reason'] or '无原因'}",
            )
        yield event.plain_result("\n".join(lines))

    @filter.command("清空违规")
    async def clear_violations_command(self, event: AstrMessageEvent):
        """清空成员违规记录，仅群主可用。"""
        if not self._claim_event(event):
            return
        async for result in self._run_safely(
            event,
            self._handle_clear_violations,
            action="清空违规记录",
        ):
            yield result

    async def _handle_clear_violations(
        self,
        event: AstrMessageEvent,
    ) -> AsyncGenerator[Any, None]:
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 该指令只能在群聊中使用。")
            return
        if not await self._is_group_owner(event):
            yield event.plain_result("❌ 该指令仅限群主使用。")
            return

        target_id, target_name = self._resolve_target(event, group_id)
        if not target_id:
            yield event.plain_result("❌ 用法：/清空违规 @某人")
            return

        counts = self.db.clear_violations(group_id, target_id)
        yield event.plain_result(
            f"✅ 已清空 {target_name} 的违规记录："
            f"警告 {counts['warnings']} 条，惩罚 {counts['punishments']} 条。",
        )

    # ------------------------------------------------------------------
    # v4.0.0 群管：公告
    # ------------------------------------------------------------------
    @filter.command("发布公告")
    async def publish_announcement_command(self, event: AstrMessageEvent):
        """发布群公告并 @全体成员，仅群主/管理员可用。"""
        if not self._claim_event(event):
            return
        async for result in self._run_safely(
            event,
            self._handle_publish_announcement,
            action="发布公告",
        ):
            yield result

    async def _handle_publish_announcement(
        self,
        event: AstrMessageEvent,
    ) -> AsyncGenerator[Any, None]:
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 该指令只能在群聊中使用。")
            return
        if not await self._is_group_admin(event):
            yield event.plain_result("❌ 该指令仅限群主/管理员使用。")
            return

        content = self._command_remainder(event, "发布公告").strip()
        if not content:
            yield event.plain_result("❌ 用法：/发布公告 内容")
            return
        if len(content) > 1000:
            yield event.plain_result("❌ 公告内容不能超过 1000 个字符。")
            return

        result = self.db.add_announcement(
            group_id,
            content,
            publisher_id=str(event.get_sender_id() or ""),
        )
        chain = MessageChain()
        chain.at("全体成员", "all")
        chain.message(f"\n📢 群公告 #{result['id']}\n{content}")

        if hasattr(event, "chain_result"):
            # AstrBot 的 chain_result 内部会对 result.chain 执行 len()，
            # 因此必须传入 MessageChain.chain（组件列表），不能传 MessageChain 本身，
            # 否则会抛 TypeError: object of type 'MessageChain' has no len()
            yield event.chain_result(chain.chain)
        else:
            yield event.plain_result(f"📢 群公告 #{result['id']}\n@全体成员\n{content}")

    @filter.command("公告列表")
    async def announcement_list_command(self, event: AstrMessageEvent):
        """查看历史公告。"""
        if not self._claim_event(event):
            return
        async for result in self._run_safely(
            event,
            self._handle_announcement_list,
            action="查询公告",
        ):
            yield result

    async def _handle_announcement_list(
        self,
        event: AstrMessageEvent,
    ) -> AsyncGenerator[Any, None]:
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 该指令只能在群聊中使用。")
            return

        announcements = self.db.list_announcements(group_id, 10)
        if not announcements:
            yield event.plain_result("📋 本群还没有发布过公告。")
            return

        lines = [f"📋 最近公告（{len(announcements)} 条）"]
        for item in announcements:
            if item["is_scheduled"]:
                status = "已发送" if item["sent"] else f"待发送 {item['scheduled_at']}"
            else:
                status = item["created_at"]
            content = str(item["content"]).replace("\n", " ")
            if len(content) > 40:
                content = content[:40] + "…"
            lines.append(f"#{item['id']} [{status}] {content}")
        yield event.plain_result("\n".join(lines))

    @filter.command("删除公告")
    async def delete_announcement_command(self, event: AstrMessageEvent):
        """删除公告，仅群主/管理员可用。"""
        if not self._claim_event(event):
            return
        async for result in self._run_safely(
            event,
            self._handle_delete_announcement,
            action="删除公告",
        ):
            yield result

    async def _handle_delete_announcement(
        self,
        event: AstrMessageEvent,
    ) -> AsyncGenerator[Any, None]:
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 该指令只能在群聊中使用。")
            return
        if not await self._is_group_admin(event):
            yield event.plain_result("❌ 该指令仅限群主/管理员使用。")
            return

        raw = self._command_remainder(event, "删除公告").strip().lstrip("#")
        if not raw.isdigit():
            yield event.plain_result("❌ 用法：/删除公告 公告ID")
            return

        announcement_id = int(raw)
        if self.db.delete_announcement(group_id, announcement_id):
            yield event.plain_result(f"✅ 已删除公告 #{announcement_id}。")
        else:
            yield event.plain_result(f"❌ 没有找到公告 #{announcement_id}。")

    @filter.command("定时公告")
    async def schedule_announcement_command(self, event: AstrMessageEvent):
        """发布定时公告，仅群主/管理员可用。"""
        if not self._claim_event(event):
            return
        async for result in self._run_safely(
            event,
            self._handle_schedule_announcement,
            action="定时公告",
        ):
            yield result

    async def _handle_schedule_announcement(
        self,
        event: AstrMessageEvent,
    ) -> AsyncGenerator[Any, None]:
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 该指令只能在群聊中使用。")
            return
        if not await self._is_group_admin(event):
            yield event.plain_result("❌ 该指令仅限群主/管理员使用。")
            return

        parsed = self._parse_schedule_time(
            self._command_remainder(event, "定时公告"),
        )
        if parsed is None:
            yield event.plain_result(
                "❌ 用法：/定时公告 时间 内容\n"
                "时间格式：2026-10-22 20:00 或 20:00（今天，已过则明天）",
            )
            return

        scheduled_at, content = parsed
        if not content:
            yield event.plain_result("❌ 请填写公告内容。")
            return

        result = self.db.add_announcement(
            group_id,
            content,
            publisher_id=str(event.get_sender_id() or ""),
            scheduled_at=scheduled_at.strftime("%Y-%m-%d %H:%M:%S"),
        )
        yield event.plain_result(
            f"✅ 已创建定时公告 #{result['id']}，"
            f"将于 {result['scheduled_at']} 发送。",
        )

    # ------------------------------------------------------------------
    # v4.0.0 群管：群数据统计
    # ------------------------------------------------------------------
    @filter.command("群数据")
    async def group_stats_command(self, event: AstrMessageEvent):
        """查看班级群整体数据。"""
        if not self._claim_event(event):
            return
        async for result in self._run_safely(
            event,
            self._handle_group_stats,
            action="查询群数据",
        ):
            yield result

    async def _handle_group_stats(
        self,
        event: AstrMessageEvent,
    ) -> AsyncGenerator[Any, None]:
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 该指令只能在群聊中使用。")
            return

        stats = self.db.get_group_stats(group_id)
        settings = self._get_mod_settings(group_id)
        enabled = [
            AUTO_MOD_LABELS[key]
            for key, value in settings.items()
            if value.get("enabled")
        ]
        text = (
            "📊 班级群数据\n"
            f"总人数：{stats['total_users']}\n"
            f"今日活跃：{stats['active_today']} 人\n"
            f"今日发言：{stats['today_messages']} 条\n"
            f"积分总量：{self._fmt_points(stats['points_total'])}"
            f"（人均 {self._fmt_points(stats['points_average'])}）\n"
            f"违规次数：{stats['violation_total']}\n"
            f"警告 {stats['warn_count']} 次 · 禁言 {stats['mute_count']} 次"
            f" · 踢出 {stats['kick_count']} 次\n"
            f"违禁词：{stats['banned_word_count']} 个 · "
            f"白名单：{stats['whitelist_count']} 人\n"
            f"自动审核：{'、'.join(enabled) if enabled else '全部关闭'}"
        )
        yield event.plain_result(text)

    @filter.command("活跃榜")
    async def active_rank_command(self, event: AstrMessageEvent):
        """查看今日发言活跃度排名。"""
        if not self._claim_event(event):
            return
        async for result in self._run_safely(
            event,
            self._handle_active_rank,
            action="查询活跃榜",
        ):
            yield result

    async def _handle_active_rank(
        self,
        event: AstrMessageEvent,
    ) -> AsyncGenerator[Any, None]:
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 该指令只能在群聊中使用。")
            return

        rows = self.db.get_active_leaderboard(group_id, limit=10)
        if not rows:
            yield event.plain_result("📊 今天还没有人发言哦～")
            return

        medals = ["🥇", "🥈", "🥉"]
        lines = ["📊 今日活跃榜"]
        for index, row in enumerate(rows):
            name = self._display_name(row["nickname"], row["user_id"])
            prefix = medals[index] if index < len(medals) else f"{index + 1}."
            lines.append(f"{prefix} {name}：{row['message_count']} 条")
        yield event.plain_result("\n".join(lines))

    @filter.command("违规榜")
    async def violation_rank_command(self, event: AstrMessageEvent):
        """查看违规次数排名。"""
        if not self._claim_event(event):
            return
        async for result in self._run_safely(
            event,
            self._handle_violation_rank,
            action="查询违规榜",
        ):
            yield result

    async def _handle_violation_rank(
        self,
        event: AstrMessageEvent,
    ) -> AsyncGenerator[Any, None]:
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 该指令只能在群聊中使用。")
            return

        rows = self.db.get_violation_leaderboard(group_id, limit=10)
        if not rows:
            yield event.plain_result("✅ 本群暂无违规记录。")
            return

        lines = ["📊 违规榜"]
        for index, row in enumerate(rows):
            name = self._display_name(row["nickname"], row["user_id"])
            lines.append(
                f"{index + 1}. {name}：{row['violation_count']} 次"
                f"（警告 {row['warn_count']} / 禁言 {row['mute_count']}"
                f" / 踢出 {row['kick_count']}）",
            )
        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------------------------
    # v4.0.0 群管：白名单
    # ------------------------------------------------------------------
    @filter.command("添加白名单")
    async def add_whitelist_command(self, event: AstrMessageEvent):
        """添加白名单成员，仅群主/管理员可用。"""
        if not self._claim_event(event):
            return
        async for result in self._run_safely(
            event,
            self._handle_add_whitelist,
            action="添加白名单",
        ):
            yield result

    async def _handle_add_whitelist(
        self,
        event: AstrMessageEvent,
    ) -> AsyncGenerator[Any, None]:
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 该指令只能在群聊中使用。")
            return
        if not await self._is_group_admin(event):
            yield event.plain_result("❌ 该指令仅限群主/管理员使用。")
            return

        target_id, target_name = self._resolve_target(event, group_id)
        if not target_id:
            yield event.plain_result("❌ 用法：/添加白名单 @某人")
            return

        if not self.db.add_whitelist(
            group_id,
            target_id,
            added_by=str(event.get_sender_id() or ""),
        ):
            yield event.plain_result(f"ℹ️ {target_name} 已经在白名单中。")
            return

        self._invalidate_whitelist_cache(group_id)
        yield event.plain_result(
            f"✅ 已将 {target_name} 加入白名单，免于违禁词与自动审核。",
        )

    @filter.command("移除白名单")
    async def remove_whitelist_command(self, event: AstrMessageEvent):
        """移除白名单成员，仅群主/管理员可用。"""
        if not self._claim_event(event):
            return
        async for result in self._run_safely(
            event,
            self._handle_remove_whitelist,
            action="移除白名单",
        ):
            yield result

    async def _handle_remove_whitelist(
        self,
        event: AstrMessageEvent,
    ) -> AsyncGenerator[Any, None]:
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 该指令只能在群聊中使用。")
            return
        if not await self._is_group_admin(event):
            yield event.plain_result("❌ 该指令仅限群主/管理员使用。")
            return

        target_id, target_name = self._resolve_target(event, group_id)
        if not target_id:
            yield event.plain_result("❌ 用法：/移除白名单 @某人")
            return

        if not self.db.remove_whitelist(group_id, target_id):
            yield event.plain_result(f"ℹ️ {target_name} 不在白名单中。")
            return

        self._invalidate_whitelist_cache(group_id)
        yield event.plain_result(f"✅ 已将 {target_name} 移出白名单。")

    @filter.command("白名单列表")
    async def whitelist_list_command(self, event: AstrMessageEvent):
        """查看本群白名单。"""
        if not self._claim_event(event):
            return
        async for result in self._run_safely(
            event,
            self._handle_whitelist_list,
            action="查询白名单",
        ):
            yield result

    async def _handle_whitelist_list(
        self,
        event: AstrMessageEvent,
    ) -> AsyncGenerator[Any, None]:
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 该指令只能在群聊中使用。")
            return

        rows = self.db.list_whitelist(group_id)
        if not rows:
            yield event.plain_result("📋 本群白名单为空。")
            return

        lines = [f"📋 本群白名单（{len(rows)} 人）"]
        for row in rows:
            name = self._display_name(row["nickname"], row["user_id"])
            lines.append(f"· {name}（{row['user_id']}）")
        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------------------------
    # 帮助
    # ------------------------------------------------------------------
    @filter.command("帮助", alias={"菜单"})
    async def help_command(self, event: AstrMessageEvent):
        """显示插件指令帮助（/帮助 或 帮助）。"""
        if not self._claim_event(event):
            return
        async for result in self._run_safely(
            event,
            self._handle_help,
            action="获取帮助",
        ):
            yield result

    async def _handle_help(
        self,
        event: AstrMessageEvent,
        topic: str = "",
    ) -> AsyncGenerator[Any, None]:
        """智能帮助：主菜单 / 分类菜单 / 功能详情。

        用法：
        - ``/帮助``、``帮助``：显示主菜单；
        - ``/帮助 签到``：显示签到功能详情；
        - ``/帮助 群管功能``：显示该分类全部功能；
        - ``/帮助 全部``：显示完整的传统指令清单。
        """
        if not topic:
            topic = self._command_remainder(event, "帮助")
        topic = str(topic or "").strip()
        topic = self._strip_help_words(topic)

        # “全部 / 完整 / 指令大全”仍然输出传统完整帮助
        if topic not in ("", "全部", "完整", "指令大全", "全部功能"):
            text, category_id = self._render_help(topic)
            group_id = self._get_group_id(event)
            user_id = str(event.get_sender_id() or "").strip()
            if group_id and user_id and category_id:
                self._set_context(
                    group_id,
                    user_id,
                    CONTEXT_CATEGORY,
                    {"category": category_id},
                )
            elif group_id and user_id:
                self._set_context(group_id, user_id, CONTEXT_MENU)
            yield event.plain_result(text)
            return

        if topic == "":
            # 主菜单：按配置输出分类菜单或平铺菜单
            config = self._reload_config()
            text = (
                self._render_flat_menu()
                if config.get("menu_style") == "flat"
                or not config.get("smart_menu", True)
                else self._render_main_menu()
            )
            group_id = self._get_group_id(event)
            user_id = str(event.get_sender_id() or "").strip()
            if group_id and user_id:
                self._set_context(group_id, user_id, CONTEXT_MENU)
            yield event.plain_result(text)
            return

        # 以下为 /帮助 全部 的完整指令清单
        text = (
            "📖 班级管理插件 v1.5.4 · 完整指令\n"
            "\n"
            "【签到与积分】\n"
            "/签到 — 每日签到（也可直接发送“签到”）\n"
            "/积分 — 查看我的积分（也可发送“我的积分”）\n"
            "/我的信息 — 积分、经验、等级、称号与升级进度\n"
            "/排行榜 — 查看班级积分榜（也可发送“积分排行”）\n"
            "/积分明细 — 最近 20 条积分变动记录\n"
            "/帮助 — 显示本帮助（也可直接发送“帮助”）\n"
            "\n"
            "【积分商城】\n"
            "/商城 — 查看本群商品（也可直接发送“商城”）\n"
            "/兑换 商品编号或名称 — 使用积分兑换商品\n"
            "/兑换记录 — 查看个人兑换历史\n"
            "\n"
            "【抽奖系统】\n"
            f"/抽奖 — 消耗 {LOTTERY_COST} 积分抽奖一次\n"
            "/奖池 — 查看奖品概率与剩余库存\n"
            "/抽奖记录 — 查看个人抽奖历史\n"
            f"· 连续 {LOTTERY_PITY} 次未中奖后，下一次必定中奖（保底）\n"
            "\n"
            "【每日任务】\n"
            "/任务 — 查看今日任务与完成进度\n"
            "/任务列表 — 查看本群全部任务\n"
            "· 签到、发言、连续签到、积分任务自动检测并发奖\n"
            "\n"
            "【成就系统】\n"
            "/成就 — 查看个人已达成成就\n"
            "/成就列表 — 查看全部成就与达成条件\n"
            "\n"
            "【积分转账】\n"
            "/转账 @成员 数量 — 转账积分给其他成员\n"
            "/转账记录 — 查看个人转账历史\n"
            f"· 手续费 {int(TRANSFER_FEE_RATE * 100)}%，最低 "
            f"{TRANSFER_MIN_FEE} 积分\n"
            f"· 每日限额 {TRANSFER_DAILY_LIMIT} 积分，"
            f"注册满 {TRANSFER_MIN_AGE_DAYS} 天才能转账\n"
            "\n"
            "【管理员指令】\n"
            "/添加商品 名称 所需积分 库存 — 添加商品\n"
            "/删除商品 编号或名称 — 删除商品\n"
            "/商城管理 — 查看商品与兑换统计\n"
            "/添加奖品 名称 概率 库存 — 添加抽奖奖品\n"
            "/删除奖品 编号或名称 — 删除抽奖奖品\n"
            "/抽奖统计 — 查看抽奖数据\n"
            "/添加任务 名称 类型 [目标次数] 积分 经验 — 添加任务\n"
            "/删除任务 编号或名称 — 删除任务\n"
            "/清零 @成员 — 清零该成员积分\n"
            "/加积分 @成员 数量 — 手动增加积分\n"
            "/减积分 @成员 数量 — 手动减少积分\n"
            "/设置签到奖励 数量 — 设置本群签到基础奖励\n"
            "/开启签到提醒 — 每天 08:00 提醒未签到成员\n"
            "/关闭签到提醒 — 关闭本群签到提醒\n"
            "\n"
            "【违禁词管理】\n"
            "/添加违禁词 词语 [惩罚类型] [时长] — 添加违禁词\n"
            "· 惩罚类型：警告 / 禁言 / 踢出 / 积分扣除\n"
            "/删除违禁词 词语 — 删除违禁词\n"
            "/违禁词列表 — 查看本群全部违禁词（也可直接发送）\n"
            "· 命中违禁词后自动执行对应惩罚并记录违规\n"
            "\n"
            "【自动审核】\n"
            "/开启防刷屏、/关闭防刷屏 — 短时间内重复发言检测\n"
            "/设置刷屏阈值 次数 秒数 — 默认 5 条 / 10 秒\n"
            "/开启防广告、/关闭防广告 — 链接、二维码、@全体成员检测\n"
            "/设置广告惩罚 分钟 — 默认禁言 60 分钟\n"
            "/开启夜间模式、/关闭夜间模式 — 0~6 点自动禁言\n"
            "\n"
            "【成员管理】\n"
            "/禁言 @成员 分钟 — 禁言成员（平台不支持时自动降级）\n"
            "/解除禁言 @成员 — 解除成员禁言\n"
            "/踢出 @成员 — 踢出成员（仅 OneBot）\n"
            "/警告 @成员 原因 — 警告并记录\n"
            "/积分惩罚 @成员 数量 [原因] — 扣除积分并记录\n"
            "/手动惩罚 @成员 警告|扣分|黑名单 — 手动执行惩罚\n"
            "/惩罚记录 [@成员] — 查看警告与惩罚历史\n"
            "/成员信息 @成员 — 查看积分、等级与违规记录\n"
            "/违规记录 @成员 — 查看违规历史\n"
            "/清空违规 @成员 — 清空违规记录（仅群主）\n"
            "\n"
            "【平台适配】\n"
            "/平台信息 — 查看当前平台与禁言/踢人/撤回能力\n"
            "· OneBot（NapCat / go-cqhttp）：支持禁言、踢人、撤回\n"
            "· QQ 官方机器人：不支持直接禁言，自动降级为警告 + 扣分\n"
            "· Telegram：通过 restrict_chat_member 禁言/解除禁言\n"
            "· 降级策略与积分扣除可在 config.yaml 的 punishment 中调整\n"
            "\n"
            "【群公告】\n"
            "/发布公告 内容 — 发布公告并 @全体成员\n"
            "/公告列表 — 查看历史公告（也可直接发送）\n"
            "/删除公告 公告ID — 删除公告\n"
            "/定时公告 时间 内容 — 定时发布，如 2026-10-22 20:30\n"
            "\n"
            "【群数据统计】\n"
            "/群数据 — 总人数、活跃、违规、积分总量等\n"
            "/活跃榜 — 今日发言活跃度排名\n"
            "/违规榜 — 违规次数排名\n"
            "\n"
            "【白名单】\n"
            "/添加白名单 @成员 — 免受违禁词与自动审核检测\n"
            "/移除白名单 @成员 — 移出白名单\n"
            "/白名单列表 — 查看白名单\n"
            "\n"
            "【积分规则】\n"
            "· 每日签到 +1（本群基础奖励可用管理员指令调整）\n"
            "· 连续签到 7 天额外 +3，连续签到 30 天额外 +10\n"
            f"· 群聊有效消息 +{CHAT_POINT_PER_MSG}，"
            f"每日上限 {self._fmt_points(CHAT_DAILY_LIMIT)} 分，"
            "每分钟最多计 1 条\n"
            "\n"
            "【经验与等级】\n"
            f"· 签到 +{SIGN_EXP_REWARD} 经验，聊天每条 "
            f"+{CHAT_EXP_PER_MSG} 经验（每日上限 {CHAT_EXP_DAILY_LIMIT}）\n"
            "· 等级 = int(sqrt(经验 / 100)) + 1\n"
            "· 称号：菜鸟学徒 → 初级学者 → 中级学者 → 高级学者 → "
            "学霸 → 学神 → 传奇学帝"
        )
        yield event.plain_result(text)

    # ------------------------------------------------------------------
    # 智能菜单与帮助渲染
    # ------------------------------------------------------------------
    @staticmethod
    def _strip_help_words(topic: str) -> str:
        """去掉“帮助 / 菜单 / 帮助 签到 / 签到 帮助”等前后缀。"""
        topic = str(topic or "").strip()
        words = ("帮助", "菜单", "help", "HELP")
        for word in words:
            if topic.startswith(word):
                topic = topic[len(word) :].strip()
            if topic.endswith(word):
                topic = topic[: -len(word)].strip()
        return topic.strip(" ：:，,。")

    @staticmethod
    def _render_main_menu() -> str:
        """渲染主菜单：只展示 6 个分类入口，避免菜单过长。"""
        lines = [
            "📋 班级助手 v1.5.4",
            "",
            "【快捷入口】",
            " | ".join(SMART_QUICK_ENTRIES),
            "",
            "【功能分类】",
            "回复数字或功能名进入：",
        ]
        for category in SMART_CATEGORIES.values():
            lines.append(
                f"{category['emoji']} {category['name']} → {category['desc']}",
            )
        lines.extend(
            [
                "",
                '💡 也可以直接说“我想签到”或“查看积分”',
                "回复「帮助 功能名」查看详细说明",
            ],
        )
        return "\n".join(lines)

    @staticmethod
    def _render_flat_menu() -> str:
        """渲染平铺菜单（menu_style=flat 时使用）。"""
        lines = ["📋 班级助手 v1.5.4 · 全部功能", ""]
        for cid, category in SMART_CATEGORIES.items():
            lines.append(f"【{cid}. {category['name']}】")
            for entry in category["entries"]:
                usage = entry.get("usage", "")
                suffix = f"（{usage}）" if usage else ""
                lines.append(f"· {entry['title']}{suffix}")
            lines.append("")
        lines.append('直接输入关键词即可使用，回复「帮助 功能名」查看说明')
        return "\n".join(lines)

    @staticmethod
    def _render_category_menu(category_id: str) -> str:
        """渲染二级分类菜单，功能带数字编号。"""
        category = SMART_CATEGORIES.get(category_id)
        if category is None:
            return "❌ 没有找到该分类，回复「帮助」返回主菜单。"
        lines = [
            f"{category['emoji']} {category['name']}",
            "",
            "回复数字选择功能：",
        ]
        for index, entry in enumerate(category["entries"], start=1):
            usage = entry.get("usage", "")
            suffix = f"（{usage}）" if usage else ""
            lines.append(f"{index}. {entry['title']}{suffix}")
        lines.extend(["0. 返回主菜单", "", "回复 0 返回主菜单"])
        return "\n".join(lines)

    @staticmethod
    def _match_category(topic: str) -> str:
        """按编号 / 名称 / 简称匹配分类，返回分类编号。"""
        topic = str(topic or "").strip()
        if topic in SMART_CATEGORIES:
            return topic

        aliases = {
            "签到积分": "1",
            "签到积分系统": "1",
            "积分系统": "1",
            "签到": "1",
            "商城兑换": "2",
            "商城兑换系统": "2",
            "商城": "2",
            "兑换": "2",
            "等级成就": "3",
            "等级成就系统": "3",
            "等级": "3",
            "成就": "3",
            "群管功能": "4",
            "群管系统": "4",
            "群管": "4",
            "管理群": "4",
            "个人中心": "5",
            "个人": "5",
            "我的": "5",
            "管理后台": "6",
            "后台": "6",
            "管理": "6",
        }
        if topic in aliases:
            return aliases[topic]

        for cid, category in SMART_CATEGORIES.items():
            if topic == category["key"]:
                return cid
        return ""

    def _render_help(self, topic: str) -> tuple[str, str]:
        """返回 (帮助文本, 可选的分类编号)。"""
        topic = str(topic or "").strip()
        if not topic:
            return self._render_main_menu(), ""

        # 1. 精确功能名
        if topic in SMART_FUNCTION_HELP:
            return self._render_function_help(topic), ""

        # 2. 关键词别名（例如“打卡” -> 签到）
        action = SMART_KEYWORDS.get(topic)
        if action:
            title = self._action_title(action)
            if title in SMART_FUNCTION_HELP:
                return self._render_function_help(title), ""

        # 3. 分类名 / 编号
        category_id = self._match_category(topic)
        if category_id:
            return self._render_category_menu(category_id), category_id

        # 4. 模糊匹配功能名
        matches = [
            name
            for name in SMART_FUNCTION_HELP
            if topic in name or name in topic
        ]
        if len(matches) == 1:
            return self._render_function_help(matches[0]), ""
        if len(matches) > 1:
            lines = ["🔍 找到多个相关功能，请回复更精确的名称："]
            for name in matches[:8]:
                lines.append(f"· {name}")
            return "\n".join(lines), ""

        return (
            f"❌ 没有找到「{topic}」相关功能。\n"
            "回复「帮助」查看主菜单，或直接发送“签到”“积分”“商城”等关键词。"
        ), ""

    @staticmethod
    def _render_function_help(name: str) -> str:
        """渲染单个功能的详细说明。"""
        usage, desc, category = SMART_FUNCTION_HELP[name]
        return (
            f"📖 功能说明：{name}\n"
            f"分类：{category}\n"
            f"用法：{usage}\n"
            f"说明：{desc}"
        )

    @staticmethod
    def _action_title(action: str) -> str:
        """动作键 -> 功能显示名（用于帮助模糊匹配）。"""
        return {
            "sign_in": "签到",
            "points": "查询积分",
            "leaderboard": "排行榜",
            "my_info": "我的信息",
            "shop": "查看商城",
            "exchange_logs": "兑换记录",
            "lottery": "抽奖一次",
            "lottery_prizes": "查看奖池",
            "lottery_logs": "抽奖记录",
            "tasks": "今日任务",
            "task_list": "任务列表",
            "achievements": "我的成就",
            "achievement_list": "全部成就",
            "transfer_logs": "转账记录",
            "points_logs": "积分明细",
            "banned_list": "违禁词列表",
            "announcement_list": "公告列表",
            "group_stats": "群数据",
            "active_rank": "活跃榜",
            "violation_rank": "违规榜",
            "whitelist_list": "白名单列表",
            "platform_info": "平台信息",
            "punish_logs": "惩罚记录",
            "manual_punish": "手动惩罚",
            "points_punish": "积分惩罚",
            "trigger_mode_show": "触发方式",
            "help": "帮助",
        }.get(action, action)

    def _smart_action_map(
        self,
    ) -> dict[str, tuple[Callable[..., AsyncGenerator[Any, None]], str]]:
        """动作键 -> (处理函数, 操作名称) 映射。"""
        return {
            "sign_in": (self._handle_sign_in, "签到"),
            "points": (self._handle_my_points, "查询积分"),
            "leaderboard": (self._handle_leaderboard, "查询排行榜"),
            "my_info": (self._handle_my_info, "查询我的信息"),
            "shop": (self._handle_shop, "查看商城"),
            "exchange_logs": (self._handle_exchange_logs, "查询兑换记录"),
            "lottery": (self._handle_lottery, "抽奖"),
            "lottery_prizes": (self._handle_lottery_prizes, "查看奖池"),
            "lottery_logs": (self._handle_lottery_logs, "查询抽奖记录"),
            "tasks": (self._handle_tasks, "查看任务"),
            "task_list": (self._handle_task_list, "查看任务列表"),
            "achievements": (self._handle_achievements, "查看成就"),
            "achievement_list": (self._handle_achievement_list, "查看成就列表"),
            "transfer_logs": (self._handle_transfer_logs, "查询转账记录"),
            "points_logs": (self._handle_points_logs, "查询积分明细"),
            "banned_list": (self._handle_banned_word_list, "查询违禁词"),
            "announcement_list": (self._handle_announcement_list, "查看公告列表"),
            "group_stats": (self._handle_group_stats, "查看群数据"),
            "active_rank": (self._handle_active_rank, "查看活跃榜"),
            "violation_rank": (self._handle_violation_rank, "查看违规榜"),
            "whitelist_list": (self._handle_whitelist_list, "查看白名单"),
            "platform_info": (self._handle_platform_info, "查看平台信息"),
            "punish_logs": (self._handle_punish_history, "查询惩罚记录"),
            "manual_punish": (self._handle_manual_punish, "手动惩罚"),
            "points_punish": (self._handle_points_punish, "积分惩罚"),
            "trigger_mode_show": (self._handle_trigger_mode_show, "查看触发方式"),
            "help": (self._handle_help, "获取帮助"),
        }

    # ------------------------------------------------------------------
    # 上下文记忆
    # ------------------------------------------------------------------
    @staticmethod
    def _context_key(event: AstrMessageEvent) -> tuple[str, str] | None:
        """生成上下文缓存键 (group_id, user_id)。"""
        group_id = ClassManagerPlugin._get_group_id(event)
        user_id = str(event.get_sender_id() or "").strip()
        if not group_id or not user_id:
            return None
        return group_id, user_id

    def _set_context(
        self,
        group_id: str,
        user_id: str,
        kind: str,
        data: dict[str, Any] | None = None,
        timeout: int | None = None,
    ) -> None:
        """记录用户当前所在功能上下文，超时后自动失效。"""
        if timeout is None:
            config = self._reload_config()
            timeout = int(
                config.get(
                    "session_timeout",
                    config.get("context_timeout", 30),
                ),
            )
        self._contexts[(group_id, user_id)] = {
            "kind": kind,
            "data": data or {},
            "expires": time.monotonic() + max(1, int(timeout)),
        }

    def _get_context(
        self,
        group_id: str,
        user_id: str,
    ) -> dict[str, Any] | None:
        """读取未过期的上下文，过期则清除。"""
        key = (group_id, user_id)
        item = self._contexts.get(key)
        if item is None:
            return None
        if float(item.get("expires", 0)) <= time.monotonic():
            self._contexts.pop(key, None)
            return None
        return item

    # ------------------------------------------------------------------
    # 防刷屏频率限制
    # ------------------------------------------------------------------
    async def _check_trigger_rate(
        self,
        event: AstrMessageEvent,
        user_id: str,
    ) -> tuple[bool, str]:
        """检查触发频率，返回 (是否允许, 需要提示的文本)。

        规则：
        - 同一用户 ``cooldown_seconds`` 秒内最多触发 1 次；
        - 连续 5 次快速触发（1 次成功 + 4 次被拦截）进入 60 秒冷却；
        - ``admin_bypass=true`` 时群主/管理员不受限制。
        """
        config = self._reload_config()
        interval = int(config.get("cooldown_seconds", 3))
        if interval <= 0:
            return True, ""

        if config.get("admin_bypass") and await self._is_group_admin(event):
            return True, ""

        now = time.monotonic()
        until = self._trigger_cooldowns.get(user_id, 0.0)
        if until > now:
            return False, ""

        times = self._trigger_times.setdefault(user_id, deque())
        while times and now - times[0] >= interval:
            times.popleft()

        if times:
            hits = self._trigger_hits.get(user_id, 0) + 1
            self._trigger_hits[user_id] = hits
            # 第 1 次成功 + 后续 4 次拦截 = 连续 5 次快速触发
            if hits >= 4:
                self._trigger_cooldowns[user_id] = now + 60
                self._trigger_hits[user_id] = 0
                self._trigger_times[user_id].clear()
                return False, "⏳ 操作太频繁，已进入 60 秒冷却，请稍后再试。"
            return False, ""

        times.append(now)
        self._trigger_hits[user_id] = 0
        return True, ""

    # ------------------------------------------------------------------
    # 无唤醒前缀的快捷指令
    # ------------------------------------------------------------------
    @filter.regex(BARE_COMMAND_PATTERN)
    async def bare_command(self, event: AstrMessageEvent):
        """处理不带“/”前缀的快捷指令。"""
        # v1.5.0：本群设置为“仅指令”时，不再响应任何无前缀消息
        group_id = self._get_group_id(event)
        if group_id and self._effective_trigger_mode(group_id) == "command":
            return

        if not self._claim_event(event):
            return

        text = (event.message_str or "").strip()
        handlers: dict[str, tuple[Callable[..., AsyncGenerator[Any, None]], str]] = {
            "签到": (self._handle_sign_in, "签到"),
            "打卡": (self._handle_sign_in, "签到"),
            "我的积分": (self._handle_my_points, "查询积分"),
            "积分排行": (self._handle_leaderboard, "查询排行榜"),
            "排行榜": (self._handle_leaderboard, "查询排行榜"),
            "我的信息": (self._handle_my_info, "查询我的信息"),
            "商城": (self._handle_shop, "查看商城"),
            "兑换记录": (self._handle_exchange_logs, "查询兑换记录"),
            "抽奖": (self._handle_lottery, "抽奖"),
            "奖池": (self._handle_lottery_prizes, "查看奖池"),
            "抽奖记录": (self._handle_lottery_logs, "查询抽奖记录"),
            "任务": (self._handle_tasks, "查看任务"),
            "任务列表": (self._handle_task_list, "查看任务列表"),
            "成就": (self._handle_achievements, "查看成就"),
            "成就列表": (self._handle_achievement_list, "查看成就列表"),
            "转账记录": (self._handle_transfer_logs, "查询转账记录"),
            "积分明细": (self._handle_points_logs, "查询积分明细"),
            "违禁词列表": (self._handle_banned_word_list, "查询违禁词"),
            "公告列表": (self._handle_announcement_list, "查看公告列表"),
            "群数据": (self._handle_group_stats, "查看群数据"),
            "活跃榜": (self._handle_active_rank, "查看活跃榜"),
            "违规榜": (self._handle_violation_rank, "查看违规榜"),
            "白名单列表": (self._handle_whitelist_list, "查看白名单"),
            "帮助": (self._handle_help, "获取帮助"),
            "菜单": (self._handle_help, "获取帮助"),
        }
        item = handlers.get(text)
        if item is None:
            return

        handler, action = item
        async for result in self._run_safely(event, handler, action=action):
            yield result

        # 记住“商城”上下文，便于下一条消息用数字直接兑换
        if action == "查看商城":
            key = self._context_key(event)
            if key:
                self._set_context(key[0], key[1], CONTEXT_SHOP)

    # ------------------------------------------------------------------
    # 智能免 @ 触发与聊天模式（v1.5.1）
    # ------------------------------------------------------------------
    @filter.regex(SMART_TRIGGER_PATTERN)
    async def smart_trigger(self, event: AstrMessageEvent):
        """智能触发入口：聊天模式、菜单、关键词、意图识别、上下文回复。

        处理顺序：
        1. 聊天前缀（/聊天、/ai、#）：去除前缀后放行给 LLM；
        2. 聊天模式中的普通消息：直接放行给 LLM；
        3. 插件指令/关键词：插件处理并阻断 LLM；
        4. 自动识别：疑问句/请求句放行给 LLM，无意义消息静默阻断。
        """
        group_id = self._get_group_id(event)
        user_id = str(event.get_sender_id() or "").strip()
        if not group_id or not user_id:
            return

        raw = (event.message_str or "").strip()
        if not raw or raw.startswith("@"):
            return

        config = self._reload_config()

        # ---------- 1. 聊天模式前缀（必须放在唤醒判断之前） ----------
        chat_prefix, chat_payload = self._match_chat_prefix(event, raw)
        if chat_prefix:
            if chat_payload:
                # 一次性聊天：去掉前缀后交给 LLM，不阻断
                self._set_chat_passthrough(event, chat_payload)
                return
            # 只发送 /聊天：进入持续聊天模式，并提示用户
            if not self._claim_event(event):
                return
            timeout = int(config.get("chat_timeout", 30))
            self._set_context(
                group_id,
                user_id,
                CONTEXT_CHAT,
                timeout=timeout,
            )
            yield event.plain_result(
                "🤖 已进入 AI 聊天模式\n"
                "直接输入消息即可对话\n"
                f'输入“退出”或 {timeout} 秒无操作自动退出',
            )
            return

        # 唤醒指令交给 @filter.command 处理，避免被“积分”等关键词误识别
        if getattr(event, "is_at_or_wake_command", False):
            return

        mode = self._effective_trigger_mode(group_id)
        prefix = raw[0] if raw[0] in TRIGGER_PREFIXES else ""
        if prefix in ("/", "／"):
            # 以 / 开头交给 AstrBot 指令系统处理
            return

        if prefix:
            # # / ！ 前缀在任何模式下都视为明确指令
            text = raw[1:].strip()
        elif mode == "command":
            return
        else:
            text = raw

        if not text or len(text) > 30:
            return

        context = self._get_context(group_id, user_id)

        # ---------- 2. 持续聊天模式 ----------
        if context and context.get("kind") == CONTEXT_CHAT:
            # 退出聊天
            if text in CHAT_EXIT_WORDS:
                self._contexts.pop((group_id, user_id), None)
                if not self._claim_event(event):
                    return
                yield event.plain_result("👋 已退出 AI 聊天模式")
                return

            # 聊天模式中，仅“精确功能词”交给插件，其余全部放行给 LLM
            help_topic = self._extract_help_topic(text)
            if help_topic is None and text not in SMART_KEYWORDS:
                context["expires"] = time.monotonic() + int(
                    config.get("chat_timeout", 30),
                )
                self._set_chat_passthrough(event, text)
                return

            # 使用插件功能后自动退出聊天模式
            self._contexts.pop((group_id, user_id), None)
            if help_topic is not None:
                if not self._claim_event(event):
                    return
                async for result in self._run_safely(
                    event,
                    self._handle_help,
                    help_topic,
                    action="获取帮助",
                ):
                    yield result
                return
            action = SMART_KEYWORDS.get(text, "")
            if action:
                async for result in self._execute_smart_action(
                    event,
                    action,
                    group_id,
                    user_id,
                ):
                    yield result
            return

        # ---------- 3. 帮助 / 菜单 ----------
        help_topic = self._extract_help_topic(text)
        if help_topic is not None:
            if not self._claim_event(event):
                return
            allowed, notice = await self._check_trigger_rate(event, user_id)
            if not allowed:
                if notice:
                    yield event.plain_result(notice)
                return
            async for result in self._run_safely(
                event,
                self._handle_help,
                help_topic,
                action="获取帮助",
            ):
                yield result
            return

        # ---------- 4. 确认上下文（“你是想签到吗？”） ----------
        if help_topic is None and not text.isdigit() and context:
            kind = context.get("kind")
            data = context.get("data") or {}
            if kind == CONTEXT_CONFIRM:
                if text in CONFIRM_YES:
                    self._contexts.pop((group_id, user_id), None)
                    action = str(data.get("action", ""))
                    async for result in self._execute_smart_action(
                        event,
                        action,
                        group_id,
                        user_id,
                        check_rate=False,
                    ):
                        yield result
                    return
                if text in CONFIRM_NO:
                    self._contexts.pop((group_id, user_id), None)
                    if not self._claim_event(event):
                        return
                    yield event.plain_result("👌 已取消")
                    return
                # 输入了其他内容：放弃确认，继续按新消息处理
                self._contexts.pop((group_id, user_id), None)

        # ---------- 5. 返回主菜单 ----------
        if context and text in RETURN_WORDS:
            if not self._claim_event(event):
                return
            self._set_context(group_id, user_id, CONTEXT_MENU)
            yield event.plain_result(self._render_main_menu())
            return

        # ---------- 6. 上下文数字快捷回复 ----------
        if text.isdigit():
            if context is not None:
                if not self._claim_event(event):
                    return
                async for result in self._handle_context_number(
                    event,
                    group_id,
                    user_id,
                    int(text),
                ):
                    yield result
                return
            # 无上下文时数字属于无意义消息，按自动识别规则处理
            self._claim_event(event)
            return

        # ---------- 7. 精确/模糊关键词与意图识别 ----------
        action = self._detect_intent(text)
        if action:
            async for result in self._execute_smart_action(
                event,
                action,
                group_id,
                user_id,
            ):
                yield result
            return

        # ---------- 8. 单字缩写确认 ----------
        if len(text) == 1 and text in SHORT_COMMAND_HINTS:
            action = SHORT_COMMAND_HINTS[text]
            title = self._action_title(action)
            if not self._claim_event(event):
                return
            self._set_context(
                group_id,
                user_id,
                CONTEXT_CONFIRM,
                {"action": action},
            )
            yield event.plain_result(
                f"🤔 你是想「{title}」吗？回复“是”确认，回复“否”取消。",
            )
            return

        # ---------- 9. 未知指令智能推荐 ----------
        suggestions = self._suggest_similar(text)
        if suggestions:
            if not self._claim_event(event):
                return
            yield event.plain_result(
                "🔍 没有找到这个功能，你是不是想：\n"
                + "、".join(suggestions)
                + "\n\n回复“帮助”查看完整菜单。",
            )
            return

        # ---------- 10. 自动识别：问句/请求放行给 LLM ----------
        if config.get("auto_detect", True) and self._is_question_or_request(text):
            # 不 claim、不 stop_event，让后续 LLM 正常回复
            return

        # ---------- 11. 普通陈述：静默阻断，避免打扰 LLM ----------
        self._claim_event(event)

    def _match_chat_prefix(
        self,
        event: AstrMessageEvent,
        raw: str,
    ) -> tuple[str, str]:
        """匹配聊天模式前缀，返回 (前缀, 去掉前缀后的内容)。

        同时检查当前 ``message_str`` 与原始 Plain 文本，兼容 AstrBot
        已经剥离“/”唤醒前缀的情况。
        """
        candidates = [raw]
        original = self._original_plain_text(event)
        if original and original != raw:
            candidates.insert(0, original)

        prefixes = self._reload_config().get(
            "chat_prefixes",
            DEFAULT_PLUGIN_CONFIG["chat_prefixes"],
        )
        ordered = sorted((str(p) for p in prefixes if str(p)), key=len, reverse=True)
        for candidate in candidates:
            for prefix in ordered:
                if candidate.startswith(prefix):
                    return prefix, candidate[len(prefix) :].strip()
        return "", ""

    @staticmethod
    def _original_plain_text(event: AstrMessageEvent) -> str:
        """拼接事件中的原始 Plain 文本。"""
        getter = getattr(event, "get_messages", None)
        if not callable(getter):
            return ""
        try:
            messages = getter() or []
        except Exception:
            return ""
        parts = []
        for component in messages:
            if isinstance(component, Comp.Plain):
                parts.append(str(getattr(component, "text", "") or ""))
        return "".join(parts).strip()

    @staticmethod
    def _set_chat_passthrough(event: AstrMessageEvent, text: str) -> None:
        """把去掉聊天前缀后的内容写回事件，放行给 LLM。"""
        try:
            event.message_str = text
        except Exception:
            pass
        message_obj = getattr(event, "message_obj", None)
        if message_obj is not None:
            for attr in ("message_str", "raw_message"):
                if hasattr(message_obj, attr):
                    try:
                        setattr(message_obj, attr, text)
                    except Exception:
                        pass
            messages = getattr(message_obj, "message", None)
            if isinstance(messages, list):
                for component in messages:
                    if isinstance(component, Comp.Plain):
                        try:
                            component.text = text
                            break
                        except Exception:
                            pass
        # 唤醒标记可能让 LLM 跳过该消息，这里显式关闭
        if getattr(event, "is_at_or_wake_command", False):
            try:
                event.is_at_or_wake_command = False
            except Exception:
                pass

    def _detect_intent(self, text: str) -> str:
        """本地意图识别：关键词 Trie + 自然语言短语 + 编辑距离纠错。"""
        word, action = self._keyword_trie.find_longest(text)
        if action and len(text) >= 2:
            return action

        config = self._reload_config()
        if not config.get("smart_menu", True):
            return ""

        for phrases, intent in SMART_INTENT_RULES:
            for phrase in phrases:
                if phrase in text:
                    return intent

        return self._correct_keyword(text)

    @staticmethod
    def _levenshtein(left: str, right: str) -> int:
        """计算两个字符串的编辑距离（用于轻量级纠错）。"""
        left = str(left or "")
        right = str(right or "")
        if left == right:
            return 0
        if not left:
            return len(right)
        if not right:
            return len(left)
        previous = list(range(len(right) + 1))
        for i, lc in enumerate(left, start=1):
            current = [i]
            for j, rc in enumerate(right, start=1):
                cost = 0 if lc == rc else 1
                current.append(
                    min(
                        previous[j] + 1,
                        current[j - 1] + 1,
                        previous[j - 1] + cost,
                    ),
                )
            previous = current
        return previous[-1]

    def _correct_keyword(self, text: str) -> str:
        """把“积份”“鉴到”等轻微错别字纠正为对应功能。"""
        text = str(text or "").strip()
        if len(text) < 2 or len(text) > 8:
            return ""
        best_action = ""
        best_distance = 99
        for keyword, action in SMART_KEYWORDS.items():
            length = len(keyword)
            if abs(len(text) - length) > 1:
                continue
            distance = self._levenshtein(text, keyword)
            threshold = 1 if length <= 3 else 2
            if distance > threshold or distance >= length:
                continue
            if distance < best_distance:
                best_distance = distance
                best_action = action
        return best_action

    def _suggest_similar(self, text: str) -> list[str]:
        """为未知短指令推荐最相似的功能（最多 3 个）。"""
        text = str(text or "").strip()
        config = self._reload_config()
        if not config.get("smart_menu", True):
            return []
        if len(text) < 2 or len(text) > 6:
            return []
        seeds = set("签积商抽任成帮菜群公转等禁白活违务到卡换")
        if not any(char in seeds for char in text):
            return []

        scored: dict[str, tuple[int, str]] = {}
        for keyword, action in SMART_KEYWORDS.items():
            distance = self._levenshtein(text, keyword)
            if distance <= 2 and distance / max(1, len(keyword)) <= 0.6:
                title = self._action_title(action)
                current = scored.get(title)
                if current is None or distance < current[0]:
                    scored[title] = (distance, title)

        ordered = sorted(scored.values(), key=lambda item: item[0])
        return [title for _distance, title in ordered[:3]]

    @staticmethod
    def _is_question_or_request(text: str) -> bool:
        """判断消息是否更像疑问句/请求句，从而交给 LLM。"""
        lowered = str(text or "").strip().lower()
        if not lowered:
            return False
        if any(word in lowered for word in QUESTION_WORDS):
            return True
        if any(word in lowered for word in REQUEST_WORDS):
            return True
        if any(word in lowered for word in GREETING_WORDS):
            return True
        return False

    async def _execute_smart_action(
        self,
        event: AstrMessageEvent,
        action: str,
        group_id: str,
        user_id: str,
        check_rate: bool = True,
    ) -> AsyncGenerator[Any, None]:
        """执行智能识别出的功能，并阻断 LLM。"""
        if not action:
            return
        if not self._claim_event(event):
            return

        if check_rate:
            allowed, notice = await self._check_trigger_rate(event, user_id)
            if not allowed:
                if notice:
                    yield event.plain_result(notice)
                return

        item = self._smart_action_map().get(action)
        if item is None:
            return
        handler, label = item
        async for result in self._run_safely(event, handler, action=label):
            yield result

        # 记录功能上下文
        if action == "shop":
            self._set_context(group_id, user_id, CONTEXT_SHOP)
        elif action == "help":
            self._set_context(group_id, user_id, CONTEXT_MENU)

    def _extract_help_topic(self, text: str) -> str | None:
        """识别“帮助”“帮助 签到”“签到 帮助”等写法。

        返回 None 表示不是帮助请求；返回字符串表示帮助主题（可为空）。
        """
        text = str(text or "").strip()
        words = ("帮助", "菜单", "help", "HELP")
        if text in words:
            return ""
        for word in words:
            # “帮助 签到”
            if text.startswith(word):
                rest = text[len(word) :].strip(" ：:，,")
                return rest
            # “签到 帮助”
            if text.endswith(word):
                rest = text[: -len(word)].strip(" ：:，,")
                if rest:
                    return rest
        return None

    async def _handle_context_number(
        self,
        event: AstrMessageEvent,
        group_id: str,
        user_id: str,
        number: int,
    ) -> AsyncGenerator[Any, None]:
        """处理上下文中的数字快捷回复。"""
        context = self._get_context(group_id, user_id)
        if context is None:
            return

        # 数字也受频率限制保护
        allowed, notice = await self._check_trigger_rate(event, user_id)
        if not allowed:
            if notice:
                yield event.plain_result(notice)
            return

        kind = context.get("kind")
        data = context.get("data") or {}

        # ---------- 主菜单：数字进入分类 ----------
        if kind == CONTEXT_MENU:
            cid = str(number)
            if cid in SMART_CATEGORIES:
                yield event.plain_result(self._render_category_menu(cid))
                self._set_context(
                    group_id,
                    user_id,
                    CONTEXT_CATEGORY,
                    {"category": cid},
                )
            return

        # ---------- 二级菜单：数字执行功能 ----------
        if kind == CONTEXT_CATEGORY:
            cid = str(data.get("category", ""))
            category = SMART_CATEGORIES.get(cid)
            if category is None:
                return
            if number == 0:
                yield event.plain_result(self._render_main_menu())
                self._set_context(group_id, user_id, CONTEXT_MENU)
                return
            if number < 1 or number > len(category["entries"]):
                return
            entry = category["entries"][number - 1]
            action = entry.get("action", "")
            usage = entry.get("usage", "")

            if not action:
                yield event.plain_result(
                    f"📖 {entry['title']}\n用法：{usage}\n"
                    "发送带参数的完整指令即可使用。",
                )
                return

            action_map = self._smart_action_map()
            item = action_map.get(action)
            if item is None:
                return
            handler, label = item
            async for result in self._run_safely(event, handler, action=label):
                yield result

            # 进入商城后切换为商城上下文，可继续用数字兑换商品
            if action == "shop":
                self._set_context(group_id, user_id, CONTEXT_SHOP)
            return

        # ---------- 商城：数字直接兑换商品 ----------
        if kind == CONTEXT_SHOP:
            if number == 0:
                yield event.plain_result(self._render_main_menu())
                self._set_context(group_id, user_id, CONTEXT_MENU)
                return
            async for result in self._run_safely(
                event,
                self._handle_exchange,
                str(number),
                action="兑换商品",
            ):
                yield result
            # 兑换后保持在商城上下文，方便继续兑换
            self._set_context(group_id, user_id, CONTEXT_SHOP)

    # ------------------------------------------------------------------
    # 本群触发方式设置（管理员）
    # ------------------------------------------------------------------
    @filter.command("触发模式")
    async def trigger_mode_command(
        self,
        event: AstrMessageEvent,
        mode: str = "",
    ):
        """查看或设置本群免 @ 触发方式。"""
        if not self._claim_event(event):
            return
        if not mode:
            mode = self._command_remainder(event, "触发模式")
        async for result in self._run_safely(
            event,
            self._handle_trigger_mode,
            mode,
            action="设置触发方式",
        ):
            yield result

    async def _handle_trigger_mode_show(
        self,
        event: AstrMessageEvent,
    ) -> AsyncGenerator[Any, None]:
        """菜单入口：仅查看本群当前触发方式。"""
        async for result in self._handle_trigger_mode(event, "", action="查看触发方式"):
            yield result

    async def _handle_trigger_mode(
        self,
        event: AstrMessageEvent,
        raw_mode: str = "",
        action: str = "设置触发方式",
    ) -> AsyncGenerator[Any, None]:
        """查看/设置本群触发方式：全部、仅指令、仅关键词。"""
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 该指令只能在群聊中使用。")
            return

        global_mode = str(
            self._reload_config().get("trigger_mode", "all"),
        )
        current = self._effective_trigger_mode(group_id)
        group_mode = self.db.get_group_trigger_mode(group_id)

        raw_mode = str(raw_mode or "").strip()
        if not raw_mode:
            source = "本群设置" if group_mode else "全局配置"
            yield event.plain_result(
                "⚙️ 本群触发方式\n"
                f"当前：{TRIGGER_MODE_LABELS.get(current, current)}（{source}）\n"
                f"全局默认：{TRIGGER_MODE_LABELS.get(global_mode, global_mode)}\n"
                "可选：/触发模式 全部 | 指令 | 关键词 | 默认",
            )
            return

        normalized = TRIGGER_MODE_ALIASES.get(raw_mode.lower())
        if normalized is None:
            yield event.plain_result(
                "❌ 无法识别该触发方式。\n"
                "用法：/触发模式 全部 | 指令 | 关键词 | 默认",
            )
            return

        if not await self._is_group_admin(event):
            yield event.plain_result("❌ 仅群主/管理员可以修改本群触发方式。")
            return

        self.db.set_group_trigger_mode(group_id, normalized)
        if normalized:
            label = TRIGGER_MODE_LABELS.get(normalized, normalized)
            yield event.plain_result(f"✅ 本群触发方式已设置为：{label}")
        else:
            yield event.plain_result(
                "✅ 已恢复为全局默认触发方式："
                f"{TRIGGER_MODE_LABELS.get(global_mode, global_mode)}",
            )

    # ------------------------------------------------------------------
    # 管理员指令：积分管理
    # ------------------------------------------------------------------
    @filter.command("清零")
    async def reset_points_command(self, event: AstrMessageEvent):
        """清零指定成员积分，仅群主/管理员可用。"""
        if not self._claim_event(event):
            return
        async for result in self._run_safely(
            event,
            self._handle_reset_points,
            action="清零积分",
        ):
            yield result

    async def _handle_reset_points(
        self,
        event: AstrMessageEvent,
    ) -> AsyncGenerator[Any, None]:
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 该指令只能在群聊中使用。")
            return
        if not await self._is_group_admin(event):
            yield event.plain_result("❌ 该指令仅限群主/管理员使用。")
            return

        target_id, target_name = self._resolve_target(event, group_id)
        if not target_id:
            yield event.plain_result(
                "❌ 请使用 @ 指定成员，例如：/清零 @张三",
            )
            return

        self.db.reset_points(group_id, target_id, target_name)
        yield event.plain_result(
            f"✅ 已将 {target_name} 的积分清零\n当前积分：0",
        )

    @filter.command("加积分")
    async def add_points_command(self, event: AstrMessageEvent, amount: str = ""):
        """手动增加成员积分，仅群主/管理员可用。"""
        if not self._claim_event(event):
            return
        async for result in self._run_safely(
            event,
            self._handle_adjust_points,
            amount,
            1,
            action="增加积分",
        ):
            yield result

    @filter.command("减积分")
    async def sub_points_command(self, event: AstrMessageEvent, amount: str = ""):
        """手动减少成员积分，仅群主/管理员可用。"""
        if not self._claim_event(event):
            return
        async for result in self._run_safely(
            event,
            self._handle_adjust_points,
            amount,
            -1,
            action="减少积分",
        ):
            yield result

    async def _handle_adjust_points(
        self,
        event: AstrMessageEvent,
        raw_amount: str = "",
        direction: int = 1,
    ) -> AsyncGenerator[Any, None]:
        """增加/减少积分的公共逻辑。"""
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 该指令只能在群聊中使用。")
            return
        if not await self._is_group_admin(event):
            yield event.plain_result("❌ 该指令仅限群主/管理员使用。")
            return

        target_id, target_name = self._resolve_target(event, group_id)
        if not target_id:
            yield event.plain_result(
                "❌ 请使用 @ 指定成员，例如：/加积分 @张三 10",
            )
            return

        amount = self._parse_points_amount(raw_amount)
        if amount is None:
            # aiocqhttp 会把 @成员 渲染成 "@昵称(QQ号)" 并作为第一个参数传给指令，
            # 导致框架绑定的 amount 不是数字。这里从原始消息中找到真正的数量。
            command = "加积分" if direction > 0 else "减积分"
            remainder = self._command_remainder(event, command)
            for token in remainder.split():
                if self._clean_target_token(token) == str(target_id):
                    continue
                parsed = self._parse_points_amount(token)
                if parsed is not None:
                    amount = parsed
                    break
        if amount is None:
            yield event.plain_result(
                "❌ 积分数量不合法，请输入大于 0 的数字，例如：/加积分 @张三 10",
            )
            return

        result = self.db.adjust_points(
            group_id,
            target_id,
            amount * direction,
            target_name,
        )
        action = "增加" if direction > 0 else "减少"
        tip = ""
        if direction < 0 and abs(result["applied"]) < amount:
            tip = "（积分最低为 0）"

        yield event.plain_result(
            f"✅ 已为 {target_name} {action} {self._fmt_points(amount)} 积分{tip}\n"
            f"当前积分：{self._fmt_points(result['new'])}",
        )

    @filter.command("设置签到奖励")
    async def set_sign_reward_command(
        self,
        event: AstrMessageEvent,
        amount: str = "",
    ):
        """修改本群签到基础积分，仅群主/管理员可用。"""
        if not self._claim_event(event):
            return
        async for result in self._run_safely(
            event,
            self._handle_set_sign_reward,
            amount,
            action="设置签到奖励",
        ):
            yield result

    async def _handle_set_sign_reward(
        self,
        event: AstrMessageEvent,
        raw_amount: str = "",
    ) -> AsyncGenerator[Any, None]:
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 该指令只能在群聊中使用。")
            return
        if not await self._is_group_admin(event):
            yield event.plain_result("❌ 该指令仅限群主/管理员使用。")
            return

        try:
            value = int(str(raw_amount).strip())
        except (TypeError, ValueError):
            value = -1
        if value < 0 or value > 1000:
            yield event.plain_result(
                "❌ 奖励数量必须是 0~1000 之间的整数，例如：/设置签到奖励 2",
            )
            return

        self.db.set_sign_reward(group_id, value)
        yield event.plain_result(
            f"✅ 本群签到基础奖励已设置为 {value} 分/次。",
        )

    @filter.command("开启签到提醒")
    async def enable_reminder_command(self, event: AstrMessageEvent):
        """开启每日 8 点签到提醒，仅群主/管理员可用。"""
        if not self._claim_event(event):
            return
        async for result in self._run_safely(
            event,
            self._handle_enable_reminder,
            action="开启签到提醒",
        ):
            yield result

    async def _handle_enable_reminder(
        self,
        event: AstrMessageEvent,
    ) -> AsyncGenerator[Any, None]:
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 该指令只能在群聊中使用。")
            return
        if not await self._is_group_admin(event):
            yield event.plain_result("❌ 该指令仅限群主/管理员使用。")
            return

        self.db.set_reminder(
            group_id,
            True,
            event.unified_msg_origin,
            event.get_platform_id(),
        )
        yield event.plain_result(
            "✅ 已开启每日 08:00 签到提醒。\n"
            "提醒会 @ 已在本群互动过、且当天尚未签到的成员。",
        )

    @filter.command("关闭签到提醒")
    async def disable_reminder_command(self, event: AstrMessageEvent):
        """关闭每日签到提醒，仅群主/管理员可用。"""
        if not self._claim_event(event):
            return
        async for result in self._run_safely(
            event,
            self._handle_disable_reminder,
            action="关闭签到提醒",
        ):
            yield result

    async def _handle_disable_reminder(
        self,
        event: AstrMessageEvent,
    ) -> AsyncGenerator[Any, None]:
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("❌ 该指令只能在群聊中使用。")
            return
        if not await self._is_group_admin(event):
            yield event.plain_result("❌ 该指令仅限群主/管理员使用。")
            return

        self.db.set_reminder(
            group_id,
            False,
            event.unified_msg_origin,
            event.get_platform_id(),
        )
        yield event.plain_result("✅ 已关闭本群签到提醒。")

    # ------------------------------------------------------------------
    # 聊天积分与经验
    # ------------------------------------------------------------------
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def collect_chat_points(self, event: AstrMessageEvent):
        """统计群聊有效消息的聊天积分与经验（升级时发送祝贺）。"""
        try:
            group_id = self._get_group_id(event)
            if not group_id:
                return

            user_id = str(event.get_sender_id() or "").strip()
            if not user_id or user_id == str(event.get_self_id() or ""):
                return

            # 指令、@机器人等唤醒消息不计入聊天积分
            if getattr(event, "is_at_or_wake_command", False):
                return

            text = (event.message_str or "").strip()
            if not text:
                return
            # 跳过指令消息，避免指令本身也被计分
            if text.startswith(("/", "#", "!")):
                return
            if text.lstrip("/").strip() in CHAT_SKIP_WORDS:
                return

            # v4.0.0：自动审核（违禁词 / 防刷屏 / 防广告 / 夜间模式）。
            # 检测本身很快，真正的禁言、踢人等平台操作放入异步任务执行，
            # 不阻塞聊天响应与积分统计。
            if self._schedule_auto_moderation(
                event,
                group_id,
                user_id,
                nickname=event.get_sender_name() or "",
                text=text,
            ):
                return

            nickname = event.get_sender_name() or ""
            self.db.ensure_user(group_id, user_id, nickname)
            result = self.db.add_chat_points(
                group_id=group_id,
                user_id=user_id,
                message=text,
                nickname=nickname,
            )
            if result.get("awarded"):
                self.logger.debug(
                    "聊天积分 +%s、经验 +%s：群 %s 用户 %s，今日积分 %s",
                    result["points"],
                    result["exp_gained"],
                    group_id,
                    user_id,
                    result["total"],
                )

            # v3.0.0：聊天消息也会推进“发言 N 条”任务与成就，即使本条未计分
            messages: list[str] = []
            if result.get("level_up"):
                name = nickname or user_id
                text = f"🎉 恭喜 {name} 升级到 Lv.{result['level']}！"
                if result.get("old_title") != result.get("title"):
                    text = (
                        f"🎉 恭喜 {name} 升级到 Lv.{result['level']}，"
                        f"获得称号「{result['title']}」！"
                    )
                messages.append(text)
            messages.extend(self._settle_rewards(group_id, user_id, nickname))
            if messages:
                yield event.plain_result("\n".join(messages))
        except Exception:
            # 聊天积分属于附加功能，出错时只记录日志，不影响正常聊天
            self.logger.debug(
                "聊天积分统计失败：\n%s",
                traceback.format_exc(),
            )

    # ------------------------------------------------------------------
    # 定时提醒
    # ------------------------------------------------------------------
    async def _reminder_loop(self) -> None:
        """每分钟检查一次，在早上 8 点给开启提醒的群发送未签到名单。"""
        while True:
            try:
                await asyncio.sleep(60)
                now = datetime.now()
                if now.hour != REMINDER_HOUR or now.minute >= REMINDER_WINDOW_MINUTES:
                    continue

                today = now.strftime("%Y-%m-%d")
                groups = self.db.list_reminder_groups(today)
                for group in groups:
                    group_id = str(group["group_id"])
                    umo = str(group["unified_msg_origin"] or "")
                    if not umo:
                        continue

                    unsigned = self.db.get_unsigned_users(group_id, today, 21)
                    # 没有未签到成员时不打扰群聊，直接标记为今日已处理
                    if not unsigned:
                        self.db.mark_reminder_sent(group_id, today)
                        continue

                    has_more = len(unsigned) > 20
                    if has_more:
                        unsigned = unsigned[:20]
                    chain = self._build_reminder_chain(unsigned, has_more)
                    try:
                        await self.context.send_message(umo, chain)
                    except Exception:
                        self.logger.error(
                            "向群 %s 发送签到提醒失败：\n%s",
                            group_id,
                            traceback.format_exc(),
                        )
                    # 无论发送是否成功，今天都不再重复尝试，避免刷屏
                    self.db.mark_reminder_sent(group_id, today)

            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.error(
                    "签到提醒任务异常：\n%s",
                    traceback.format_exc(),
                )

    def _build_reminder_chain(
        self,
        unsigned: list[dict[str, Any]],
        has_more: bool = False,
    ) -> MessageChain:
        """构造提醒消息链，用 At 组件提醒未签到成员。"""
        chain = MessageChain()
        suffix = "，仅显示前 20 位" if has_more else ""
        chain.message(
            f"📢 早上好！新的一天开始啦～\n"
            f"今天还没有签到的同学（{len(unsigned)} 位{suffix}）：\n",
        )
        for index, user in enumerate(unsigned):
            if index:
                chain.message(" ")
            uid = str(user["user_id"])
            name = self._display_name(user.get("nickname"), uid)
            chain.at(name, uid)
        chain.message(
            "\n发送 /签到 即可签到，连续签到还有额外奖励哦！",
        )
        return chain

    # ------------------------------------------------------------------
    # v4.0.0 自动审核
    # ------------------------------------------------------------------
    def _get_mod_settings(self, group_id: str) -> dict[str, dict[str, Any]]:
        """读取自动审核配置（带进程内缓存）。"""
        cached = self._mod_cache.get(group_id)
        if cached is None:
            cached = self.db.get_auto_mod_settings(group_id)
            self._mod_cache[group_id] = cached
        return cached

    def _invalidate_mod_cache(self, group_id: str) -> None:
        """清除指定群的自动审核配置缓存。"""
        self._mod_cache.pop(group_id, None)

    def _get_banned_automaton(self, group_id: str) -> AhoCorasick:
        """获取（并缓存）指定群的违禁词自动机。"""
        cached = self._banned_cache.get(group_id)
        if cached is not None:
            return cached[1]

        words = [
            str(row["word"])
            for row in self.db.list_banned_words(group_id)
        ]
        automaton = AhoCorasick(words)
        self._banned_cache[group_id] = (words, automaton)
        return automaton

    def _invalidate_banned_cache(self, group_id: str) -> None:
        """违禁词变化后清除自动机缓存，下次使用时重建。"""
        self._banned_cache.pop(group_id, None)

    def _get_whitelist_set(self, group_id: str) -> set[str]:
        """获取指定群的白名单用户集合（带进程内缓存）。"""
        cached = self._whitelist_cache.get(group_id)
        if cached is None:
            cached = {
                str(row["user_id"])
                for row in self.db.list_whitelist(group_id)
            }
            self._whitelist_cache[group_id] = cached
        return cached

    def _invalidate_whitelist_cache(self, group_id: str) -> None:
        """白名单变化后清除缓存。"""
        self._whitelist_cache.pop(group_id, None)

    def _check_spam(
        self,
        group_id: str,
        user_id: str,
        spam_config: dict[str, Any],
    ) -> int:
        """检测短时间连续发言，返回窗口内消息数（未触发返回 0）。"""
        try:
            threshold = max(2, int(spam_config.get("threshold", DEFAULT_SPAM_THRESHOLD)))
            window = max(1, int(spam_config.get("window", DEFAULT_SPAM_WINDOW)))
        except (TypeError, ValueError):
            threshold = DEFAULT_SPAM_THRESHOLD
            window = DEFAULT_SPAM_WINDOW

        now_ts = datetime.now().timestamp()
        key = (group_id, user_id)
        history = self._spam_state.setdefault(key, deque())
        while history and now_ts - history[0] > window:
            history.popleft()
        history.append(now_ts)

        if len(history) >= threshold:
            count = len(history)
            history.clear()
            return count
        return 0

    def _detect_ad(self, event: AstrMessageEvent, text: str) -> str:
        """检测广告内容，返回命中原因，未命中返回空字符串。"""
        # @全体成员
        if AD_ALL_PATTERN.search(text):
            return "@全体成员"
        try:
            for component in event.get_messages() or []:
                if isinstance(component, Comp.At):
                    qq = str(getattr(component, "qq", "") or "")
                    if qq == "all":
                        return "@全体成员"
        except Exception:
            pass

        # 链接 / 常见域名 / 群邀请链接
        if AD_LINK_PATTERN.search(text):
            return "链接或域名"
        if "加群" in text or "进群" in text:
            return "疑似拉群广告"

        # 二维码：图片组件的文件名、URL 或子类型中包含 qr / 二维码
        try:
            for component in event.get_messages() or []:
                if not isinstance(component, Comp.Image):
                    continue
                values = [
                    str(getattr(component, "file", "") or ""),
                    str(getattr(component, "url", "") or ""),
                    str(getattr(component, "sub_type", "") or ""),
                    str(getattr(component, "summary", "") or ""),
                ]
                joined = " ".join(values).lower()
                if "qrcode" in joined or "qr_code" in joined or "二维码" in joined:
                    return "二维码图片"
        except Exception:
            pass
        return ""

    def _schedule_auto_moderation(
        self,
        event: AstrMessageEvent,
        group_id: str,
        user_id: str,
        nickname: str,
        text: str,
    ) -> bool:
        """检测消息是否违规；命中时异步执行处罚并返回 True。

        检测使用内存缓存与 Aho-Corasick 自动机，耗时很短；禁言、踢出、
        发送提示等耗时操作交给 ``asyncio.create_task``，不会阻塞消息响应。
        """
        try:
            # 机器人自身、管理员、群主与白名单成员免检
            if user_id == str(event.get_self_id() or ""):
                return False
            if str(getattr(event, "role", "") or "").lower() in ("admin", "owner"):
                return False
            if user_id in self._get_whitelist_set(group_id):
                return False

            settings = self._get_mod_settings(group_id)
            now = datetime.now()
            action: dict[str, Any] | None = None

            # 1. 夜间模式：0 点 - 6 点禁止发言
            night = settings.get("night_mode", {})
            if (
                night.get("enabled")
                and NIGHT_START_HOUR <= now.hour < NIGHT_END_HOUR
            ):
                action = {
                    "kind": "night_mode",
                    "punish_type": "禁言",
                    "duration": int(
                        night.get("mute_minutes", DEFAULT_NIGHT_MUTE_MINUTES),
                    ),
                    "reason": (
                        f"夜间模式（{NIGHT_START_HOUR}:00-{NIGHT_END_HOUR}:00）"
                        "禁止发言"
                    ),
                    "delete": True,
                }

            # 2. 违禁词：Aho-Corasick 一次扫描命中全部模式
            if action is None:
                hits = self._get_banned_automaton(group_id).search(text)
                if hits:
                    rows = self.db.list_banned_words(group_id)
                    mapping = {
                        str(row["word"]).lower(): row for row in rows
                    }
                    row = mapping.get(hits[0])
                    if row is not None:
                        action = {
                            "kind": "banned_word",
                            "punish_type": str(row["punish_type"]),
                            "duration": int(row["punish_duration"] or 0),
                            "reason": f"触发违禁词「{row['word']}」",
                            "delete": True,
                        }

            # 3. 防广告：链接、二维码、@全体成员
            if action is None:
                ad_config = settings.get("anti_ad", {})
                if ad_config.get("enabled"):
                    ad_reason = self._detect_ad(event, text)
                    if ad_reason:
                        action = {
                            "kind": "anti_ad",
                            "punish_type": "禁言",
                            "duration": int(
                                ad_config.get(
                                    "mute_minutes",
                                    DEFAULT_AD_MUTE_MINUTES,
                                ),
                            ),
                            "reason": f"发送广告内容（{ad_reason}）",
                            "delete": True,
                        }

            # 4. 防刷屏：指定时间窗口内消息数达到阈值
            if action is None:
                spam_config = settings.get("anti_spam", {})
                if spam_config.get("enabled"):
                    count = self._check_spam(group_id, user_id, spam_config)
                    if count:
                        action = {
                            "kind": "anti_spam",
                            "punish_type": "禁言",
                            "duration": int(
                                spam_config.get(
                                    "mute_minutes",
                                    DEFAULT_SPAM_MUTE_MINUTES,
                                ),
                            ),
                            "reason": (
                                f"短时间内连续发言 {count} 条（刷屏）"
                            ),
                            "delete": True,
                        }

            if action is None:
                return False

            # 冷却时间：避免同一成员被连续重复处罚
            cooldown_key = (group_id, user_id, str(action["kind"]))
            now_ts = now.timestamp()
            cooldown = (
                NIGHT_PUNISH_COOLDOWN
                if action["kind"] == "night_mode"
                else AUTO_PUNISH_COOLDOWN
            )
            if now_ts - self._auto_cooldown.get(cooldown_key, 0.0) < cooldown:
                return False
            self._auto_cooldown[cooldown_key] = now_ts

            task = asyncio.create_task(
                self._apply_auto_punishment(
                    event,
                    group_id,
                    user_id,
                    nickname,
                    action,
                ),
            )
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)
            return True
        except Exception:
            self.logger.warning(
                "自动审核检测失败：\n%s",
                traceback.format_exc(),
            )
            return False

    async def _apply_auto_punishment(
        self,
        event: AstrMessageEvent,
        group_id: str,
        user_id: str,
        nickname: str,
        action: dict[str, Any],
    ) -> None:
        """在后台执行处罚：撤回消息、禁言/踢出/扣分、写记录并提示。"""
        try:
            # 先让出事件循环，确保主消息处理流程已经返回
            await asyncio.sleep(0)

            punish_type = str(action.get("punish_type") or "警告")
            duration = int(action.get("duration") or 0)
            reason = str(action.get("reason") or "")
            executor = "系统"
            config = self._reload_config().get("punishment", {})
            platform = PlatformAdapter.detect(event)
            platform_label = PlatformAdapter.label(platform)
            threshold = int(config.get("warning_threshold", 3) or 3)

            if action.get("delete"):
                await self._recall_message(event)

            executed = True
            error = ""
            skip_log = False

            if punish_type == "禁言":
                can_mute = bool(config.get("auto_mute", True)) and (
                    PlatformAdapter.supports_mute(platform)
                )
                if can_mute:
                    executed, error = await PlatformAdapter.mute(
                        event,
                        user_id,
                        max(1, duration),
                    )
                    result_text = (
                        f"禁言 {duration} 分钟"
                        if executed
                        else f"禁言失败（{error or '机器人权限不足'}）"
                    )
                else:
                    # v1.5.3：平台不支持禁言（如 QQ 官方）或关闭自动禁言时，
                    # 自动降级为记录警告 + 扣除积分
                    applied, _, warn_count = self._apply_mute_fallback(
                        group_id,
                        user_id,
                        nickname,
                        reason,
                        executor,
                        platform_label,
                    )
                    executed = False
                    skip_log = applied
                    if applied:
                        result_text = "记录警告 + 扣除积分（平台不支持禁言）"
                        if warn_count >= threshold:
                            result_text += (
                                f"，累计警告 {warn_count} 次，请管理员手动处理"
                            )
                    else:
                        result_text = "禁言失败（平台不支持且未开启降级），已记录"
            elif punish_type == "踢出":
                if not PlatformAdapter.supports_kick(platform):
                    executed = False
                    error = f"当前平台不支持踢出（{platform_label}）"
                else:
                    executed, error = await self._onebot_action(
                        event,
                        "set_group_kick",
                        group_id=group_id,
                        user_id=user_id,
                        reject_add_request=False,
                    )
            elif punish_type == "积分扣除":
                points = max(1, duration)
                result = self.db.adjust_points(
                    group_id,
                    user_id,
                    -points,
                    nickname,
                    reason=f"自动审核：{reason}",
                )
                duration = int(abs(result.get("applied", points)))
                self.logger.info(
                    "自动审核扣除积分：群 %s 用户 %s 扣除 %s",
                    group_id,
                    user_id,
                    duration,
                )
            else:
                # 警告：add_warning 内部会同时写入 punish_logs
                self.db.add_warning(group_id, user_id, reason, warned_by=executor)

            # 警告已由 add_warning 记录，降级惩罚已由 _apply_mute_fallback 记录，
            # 避免重复写入
            if punish_type != "警告" and not skip_log:
                self.db.add_punish_log(
                    group_id,
                    user_id,
                    punish_type,
                    reason,
                    duration=duration,
                    executor_id=executor,
                )

            name = nickname or user_id
            if punish_type == "踢出":
                result_text = "移出本群" if executed else f"踢出失败（{error}），已记录"
            elif punish_type == "积分扣除":
                result_text = f"扣除 {duration} 积分"
            else:
                result_text = "警告一次"

            umo = str(getattr(event, "unified_msg_origin", "") or "")
            if umo:
                chain = MessageChain()
                chain.message(
                    f"🛡️ 群管提示\n{name} {reason}，已{result_text}。",
                )
                try:
                    await self.context.send_message(umo, chain)
                except Exception:
                    self.logger.debug(
                        "发送群管处罚提示失败：\n%s",
                        traceback.format_exc(),
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.error(
                "执行自动审核处罚失败：\n%s",
                traceback.format_exc(),
            )

    async def _recall_message(self, event: AstrMessageEvent) -> None:
        """尝试撤回违规消息（平台不支持时静默忽略）。"""
        message_id = self._get_message_id(event)
        if not message_id:
            return
        group_id = self._get_group_id(event)
        await self._onebot_action(
            event,
            "delete_msg",
            group_id=group_id,
            message_id=message_id,
        )

    @staticmethod
    def _get_message_id(event: AstrMessageEvent) -> int | None:
        """从平台原始消息中提取 message_id。"""
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        value = None
        if isinstance(raw, dict):
            value = raw.get("message_id")
        elif raw is not None:
            value = getattr(raw, "message_id", None)
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    async def _onebot_action(
        self,
        event: AstrMessageEvent,
        action: str,
        **params: Any,
    ) -> tuple[bool, str]:
        """调用 OneBot/aiocqhttp 群管理接口。

        不是 aiocqhttp 平台或机器人没有权限时返回 ``(False, 原因)``，
        调用方据此给出友好提示，不会抛出异常。
        """
        bot = getattr(event, "bot", None)
        if bot is None or not hasattr(bot, "call_action"):
            return False, "当前平台不支持该操作"

        # OneBot 接口要求群号/QQ 号为整数
        for key in ("group_id", "user_id"):
            value = params.get(key)
            if isinstance(value, str) and value.isdigit():
                params[key] = int(value)

        try:
            await bot.call_action(action, **params)
            return True, ""
        except Exception as exc:
            self.logger.warning(
                "调用平台接口 %s 失败：\n%s",
                action,
                traceback.format_exc(),
            )
            return False, str(exc) or "平台接口调用失败"

    @staticmethod
    def _extract_reason(
        event: AstrMessageEvent,
        command: str,
        target_name: str = "",
        target_id: str = "",
    ) -> str:
        """从指令参数中提取原因文本，自动去掉 @、目标昵称与用户 ID。"""
        remainder = ClassManagerPlugin._command_remainder(event, command)
        cleaned: list[str] = []
        for index, token in enumerate(remainder.split()):
            if token.startswith("@") or "[CQ:at" in token:
                continue
            if target_id and ClassManagerPlugin._clean_target_token(token) == str(
                target_id
            ):
                continue
            if index == 0 and target_name and token == target_name:
                continue
            cleaned.append(token)
        return " ".join(cleaned).strip() or "未填写原因"

    @staticmethod
    def _parse_schedule_time(raw: str) -> tuple[datetime, str] | None:
        """解析定时公告参数，返回 (发送时间, 内容)。

        支持两种格式：
        - ``2026-10-22 20:00 内容``
        - ``20:00 内容``（今天，若已过则顺延到明天）
        """
        text = str(raw or "").strip()
        if not text:
            return None

        full = re.match(
            r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})[ T](\d{1,2}):(\d{2})\s+(.+)$",
            text,
            re.S,
        )
        now = datetime.now()
        try:
            if full:
                scheduled = datetime(
                    int(full.group(1)),
                    int(full.group(2)),
                    int(full.group(3)),
                    int(full.group(4)),
                    int(full.group(5)),
                )
                content = full.group(6).strip()
            else:
                short = re.match(r"^(\d{1,2}):(\d{2})\s+(.+)$", text, re.S)
                if not short:
                    return None
                scheduled = now.replace(
                    hour=int(short.group(1)),
                    minute=int(short.group(2)),
                    second=0,
                    microsecond=0,
                )
                if scheduled <= now:
                    scheduled += timedelta(days=1)
                content = short.group(3).strip()
        except ValueError:
            return None

        if scheduled <= now:
            return None
        return scheduled, content

    async def _announcement_loop(self) -> None:
        """每 30 秒检查一次定时公告，到点后主动发送。"""
        while True:
            try:
                await asyncio.sleep(30)
                due = self.db.get_due_announcements()
                for item in due:
                    group_id = str(item["group_id"])
                    umo = self.db.get_group_origin(group_id)
                    if not umo:
                        self.logger.warning(
                            "定时公告 #%s 无法发送：群 %s 缺少会话信息",
                            item["id"],
                            group_id,
                        )
                        self.db.mark_announcement_sent(int(item["id"]))
                        continue

                    chain = MessageChain()
                    chain.at("全体成员", "all")
                    chain.message(
                        f"\n📢 群公告 #{item['id']}\n{item['content']}",
                    )
                    try:
                        await self.context.send_message(umo, chain)
                    except Exception:
                        self.logger.error(
                            "发送定时公告 #%s 失败：\n%s",
                            item["id"],
                            traceback.format_exc(),
                        )
                    # 无论成功与否都标记已发送，避免异常公告反复刷屏
                    self.db.mark_announcement_sent(int(item["id"]))
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.error(
                    "定时公告任务异常：\n%s",
                    traceback.format_exc(),
                )

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------
    @staticmethod
    def _get_group_id(event: AstrMessageEvent) -> str:
        """获取群 ID，私聊时返回空字符串。"""
        return str(event.get_group_id() or "").strip()

    def _claim_event(
        self,
        event: AstrMessageEvent,
        block_llm: bool = True,
    ) -> bool:
        """防止同一条消息被重复处理，并在需要时阻断后续 LLM 调用。

        v1.5.1：所有由插件成功处理的指令都会调用 ``event.stop_event()``，
        这样插件回复后不会再触发一次大模型回复。聊天模式与自动识别中
        判定为“应交给 LLM”的消息则不会调用本方法来阻断。
        """
        if event.get_extra("class_manager_handled", False):
            return False
        event.set_extra("class_manager_handled", True)
        if block_llm:
            self._stop_llm(event)
        return True

    def _stop_llm(self, event: AstrMessageEvent) -> None:
        """阻断当前消息继续传给 LLM（受 block_llm 配置控制）。"""
        config = self._reload_config()
        if not config.get("block_llm", True):
            return
        stop = getattr(event, "stop_event", None)
        if callable(stop):
            try:
                stop()
                return
            except Exception:
                self.logger.debug("调用 event.stop_event() 失败", exc_info=True)
        # 兼容没有 stop_event 的旧版本 AstrBot
        try:
            event.set_extra("class_manager_block_llm", True)
        except Exception:
            pass

    @staticmethod
    def _fmt_points(value: Any) -> str:
        """积分格式化：整数不带小数，小数最多保留两位。"""
        try:
            number = round(float(value), 2)
        except (TypeError, ValueError):
            return "0"
        if abs(number - int(number)) < 1e-9:
            return str(int(number))
        return f"{number:.2f}".rstrip("0").rstrip(".")

    @staticmethod
    def _display_name(nickname: Any, user_id: Any) -> str:
        """优先使用群昵称，没有昵称时回退到用户 ID。"""
        name = str(nickname or "").strip()
        return name or str(user_id)

    @staticmethod
    def _progress_bar(percent: float) -> str:
        """把百分比转换成 10 格进度条。"""
        try:
            value = max(0.0, min(100.0, float(percent)))
        except (TypeError, ValueError):
            value = 0.0
        filled = round(value / 10)
        return "█" * filled + "░" * (10 - filled)

    @staticmethod
    def _parse_points_amount(raw: Any) -> float | None:
        """解析积分数量，允许两位小数，范围 (0, 100000]。"""
        try:
            value = float(str(raw).strip())
        except (TypeError, ValueError):
            return None
        if value <= 0 or value > 100000 or not math.isfinite(value):
            return None
        return round(value, 2)

    @staticmethod
    def _parse_item_id(raw: Any) -> int | None:
        """解析商品编号，允许带 # 前缀。"""
        text = str(raw or "").strip().lstrip("#").strip()
        if not text.isdigit():
            return None
        value = int(text)
        if value <= 0 or value > 1_000_000_000:
            return None
        return value

    def _resolve_item_reference(
        self,
        items: list[dict[str, Any]],
        raw: str,
        kind: str,
        id_key: str = "id",
        name_key: str = "name",
    ) -> tuple[int | None, str]:
        """解析“编号或名称”形式的引用。

        返回 ``(编号, 错误提示)``；编号为 None 时错误提示非空。
        """
        text = str(raw or "").strip()
        if not text:
            return None, f"❌ 请提供{kind}编号或名称。"

        cleaned = text.lstrip("#").strip()
        if cleaned.isdigit():
            value = int(cleaned)
            if value <= 0 or value > 1_000_000_000:
                return None, f"❌ {kind}编号不合法。"
            return value, ""

        name = text.lstrip("@").strip().strip("「」\"'“”")
        matches = [
            item
            for item in items
            if str(item.get(name_key) or "").strip() == name
        ]
        if len(matches) == 1:
            return int(matches[0][id_key]), ""
        if len(matches) > 1:
            ids = "、".join(f"#{int(item[id_key])}" for item in matches)
            return None, f"❌ 有多个同名{kind}（{ids}），请改用编号。"
        return None, f"❌ 未找到名为「{name}」的{kind}，请先查看列表确认名称或编号。"

    @staticmethod
    def _command_remainder(event: AstrMessageEvent, command: str) -> str:
        """从原始消息中取出指令后面的参数部分。"""
        text = (event.message_str or "").strip()
        while text and text[0] in "/#!":
            text = text[1:].lstrip()
        if text.startswith(command):
            text = text[len(command) :].strip()
        return text

    def _touch_group_and_user(
        self,
        event: AstrMessageEvent,
        group_id: str,
        user_id: str,
        nickname: str = "",
    ) -> None:
        """记录群信息与用户信息，供排行榜和主动提醒使用。"""
        self.db.touch_group(
            group_id,
            event.unified_msg_origin,
            event.get_platform_id(),
        )
        self.db.ensure_user(group_id, user_id, nickname)

    def _extract_at_target(self, event: AstrMessageEvent) -> tuple[str, str]:
        """从消息链中提取被 @ 的成员 ID 与显示名。

        使用 ``@机器人`` 唤醒时，消息链的第一个 At 是机器人自己，
        必须跳过，否则会把机器人误当成操作对象。
        """
        try:
            messages = event.get_messages()
        except Exception:
            messages = []

        self_id = str(event.get_self_id() or "").strip()
        name_only = ""
        for component in messages or []:
            if isinstance(component, Comp.At):
                qq = str(getattr(component, "qq", "") or "").strip()
                name = str(getattr(component, "name", "") or "").strip()
                # 部分适配器只提供昵称、不提供 ID，先记下来按昵称查找。
                if not qq:
                    if (
                        name
                        and name not in ("全体成员", "所有人", "all")
                        and not name_only
                    ):
                        name_only = name
                    continue
                # QQ 官方机器人的 @ 目标可能是 openid（非纯数字），
                # 这里不能强制要求 qq.isdigit()，否则会误报“语法错误”。
                if qq in ("all", "0"):
                    continue
                if self_id and qq == self_id:
                    continue
                return qq, name
        return "", name_only

    @staticmethod
    def _clean_target_token(token: str) -> str:
        """清理文本形式的 @xxx / ID，去掉首尾标点。"""
        return str(token or "").strip().lstrip("@＠").strip(
            " \t\r\n，,。.、:：;；!！?？()（）[]【】<>《》\"'“”‘’"
        )

    def _resolve_target(
        self,
        event: AstrMessageEvent,
        group_id: str,
    ) -> tuple[str, str]:
        """解析管理员指令的操作对象，返回 (用户ID, 显示名)。

        支持三种写法：
        1. 消息链中的 At 组件（OneBot / QQ 官方 openid 均可）；
        2. 文本形式的 ``@昵称``（按群内昵称精确匹配）；
        3. 直接输入数字用户 ID。
        """
        target_id, at_name = self._extract_at_target(event)
        if target_id:
            stored_name = ""
            user = self.db.get_user(group_id, target_id)
            if user:
                stored_name = str(user.get("nickname") or "").strip()
            return target_id, at_name or stored_name or target_id

        # 适配器只给了 @ 昵称、没给 ID 时，按昵称精确查找唯一成员
        if at_name:
            matches = self.db.find_users_by_nickname(group_id, at_name)
            if len(matches) == 1:
                return str(matches[0]["user_id"]), at_name

        # 兜底：部分平台不会把 @ 转成 At 组件，只能从文本解析
        text = str(getattr(event, "message_str", "") or "").strip()
        tokens = text.split()
        for token in tokens:
            if not token.startswith(("@", "＠")):
                continue
            name = self._clean_target_token(token)
            if not name or name in ("全体成员", "所有人", "all"):
                continue
            matches = self.db.find_users_by_nickname(group_id, name)
            if len(matches) == 1:
                user = matches[0]
                return str(user["user_id"]), name

        # 再兜底：直接输入 5 位以上的数字用户 ID
        for token in tokens:
            cleaned = self._clean_target_token(token)
            if cleaned.isdigit() and len(cleaned) >= 5:
                user = self.db.get_user(group_id, cleaned)
                if user:
                    return cleaned, str(user.get("nickname") or cleaned)
        return "", ""

    async def _is_group_admin(self, event: AstrMessageEvent) -> bool:
        """判断发送者是否为群主/管理员。

        依次尝试：
        1. AstrMessageEvent.role；
        2. 原始消息中的 sender.role（OneBot 等平台）；
        3. 调用平台 API 查询群成员信息（aiocqhttp）。
        """
        role = str(getattr(event, "role", "") or "").lower()
        if role in ("admin", "owner"):
            return True

        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        raw_role = self._find_raw_sender_role(raw)
        if raw_role in ("admin", "owner"):
            return True

        # 兜底：通过 aiocqhttp 的 get_group_member_info 查询
        group_id = self._get_group_id(event)
        user_id = str(event.get_sender_id() or "").strip()
        bot = getattr(event, "bot", None)
        if (
            bot is not None
            and hasattr(bot, "call_action")
            and group_id.isdigit()
            and user_id.isdigit()
        ):
            try:
                info = await bot.call_action(
                    "get_group_member_info",
                    group_id=int(group_id),
                    user_id=int(user_id),
                )
                if isinstance(info, dict) and str(info.get("role", "")).lower() in (
                    "admin",
                    "owner",
                ):
                    return True
            except Exception:
                self.logger.debug(
                    "查询群成员身份失败：\n%s",
                    traceback.format_exc(),
                )

        return False

    async def _is_group_owner(self, event: AstrMessageEvent) -> bool:
        """判断发送者是否为群主（不把管理员当作群主）。"""
        role = str(getattr(event, "role", "") or "").lower()
        if role == "owner":
            return True

        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        if self._find_raw_sender_role(raw) == "owner":
            return True

        group_id = self._get_group_id(event)
        user_id = str(event.get_sender_id() or "").strip()
        bot = getattr(event, "bot", None)
        if (
            bot is not None
            and hasattr(bot, "call_action")
            and group_id.isdigit()
            and user_id.isdigit()
        ):
            try:
                info = await bot.call_action(
                    "get_group_member_info",
                    group_id=int(group_id),
                    user_id=int(user_id),
                )
                if isinstance(info, dict):
                    return str(info.get("role", "")).lower() == "owner"
            except Exception:
                self.logger.debug(
                    "查询群主身份失败：\n%s",
                    traceback.format_exc(),
                )
        return False

    @staticmethod
    def _find_raw_sender_role(raw: Any) -> str:
        """从平台原始消息对象中尽力读取发送者角色。"""
        if raw is None:
            return ""

        sender = None
        if isinstance(raw, dict):
            sender = raw.get("sender")
        else:
            sender = getattr(raw, "sender", None)
            if sender is None:
                getter = getattr(raw, "get", None)
                if callable(getter):
                    try:
                        sender = getter("sender")
                    except Exception:
                        sender = None

        if sender is None:
            return ""
        if isinstance(sender, dict):
            return str(sender.get("role", "") or "").lower()
        return str(getattr(sender, "role", "") or "").lower()

    @staticmethod
    def _normalize_role(role: Any) -> str:
        """把各平台的角色值统一为 owner / admin / member。"""
        value = str(role or "").strip().lower()
        if value in ("owner", "群主", "创建者"):
            return "owner"
        if value in ("admin", "administrator", "管理员", "管理"):
            return "admin"
        return "member"

    def _identity_text(self, role: Any) -> str:
        """返回带图标的中文身份，例如“👑 群主”。"""
        normalized = self._normalize_role(role)
        icon, label = IDENTITY_LABELS.get(normalized, IDENTITY_LABELS["member"])
        return f"{icon} {label}"

    async def _get_member_role(
        self,
        event: AstrMessageEvent,
        group_id: str,
        user_id: str,
        fallback: str = "",
    ) -> str:
        """获取群成员身份，优先缓存，其次事件字段，最后平台 API。

        身份只用于展示，因此查询失败时静默回退为普通成员，不影响主流程。
        """
        user_id = str(user_id or "").strip()
        if not group_id or not user_id:
            return self._normalize_role(fallback or "member")

        key = (group_id, user_id)
        now = time.monotonic()
        cached = self._role_cache.get(key)
        if cached and cached[1] > now:
            return cached[0]

        role = ""
        sender_id = str(event.get_sender_id() or "").strip()
        if sender_id == user_id:
            role = str(getattr(event, "role", "") or "").strip().lower()
            if not role:
                raw = getattr(
                    getattr(event, "message_obj", None),
                    "raw_message",
                    None,
                )
                role = self._find_raw_sender_role(raw)

        bot = getattr(event, "bot", None)
        if (
            not role
            and bot is not None
            and hasattr(bot, "call_action")
            and group_id.isdigit()
            and user_id.isdigit()
        ):
            try:
                info = await bot.call_action(
                    "get_group_member_info",
                    group_id=int(group_id),
                    user_id=int(user_id),
                )
                if isinstance(info, dict):
                    role = str(info.get("role", "") or "").strip().lower()
            except Exception:
                self.logger.debug(
                    "查询群成员身份失败：\n%s",
                    traceback.format_exc(),
                )

        normalized = self._normalize_role(role or fallback)
        self._role_cache[key] = (normalized, now + 300)
        return normalized

    def _settle_rewards(
        self,
        group_id: str,
        user_id: str,
        nickname: str = "",
        include_tasks: bool = True,
    ) -> list[str]:
        """结算每日任务与成就，返回可直接追加到回复中的文字行。

        任务/成就属于附加功能，任何异常都只记录日志，不影响主流程。
        """
        lines: list[str] = []

        if include_tasks:
            try:
                for task in self.db.check_tasks(group_id, user_id, nickname):
                    lines.append(
                        f"✅ 完成任务「{task['name']}」："
                        f"+{task['reward_points']} 积分 "
                        f"+{task['reward_exp']} 经验",
                    )
                    if task.get("level_up"):
                        lines.append(
                            f"🎉 恭喜升级！Lv.{task['old_level']} → "
                            f"Lv.{task['level']} {task['title']}",
                        )
            except Exception:
                self.logger.warning(
                    "任务结算失败：\n%s",
                    traceback.format_exc(),
                )

        try:
            for achievement in self.db.check_achievements(
                group_id,
                user_id,
                nickname,
            ):
                lines.append(
                    f"🏆 达成成就「{achievement['name']}」："
                    f"+{achievement['reward_points']} 积分 "
                    f"+{achievement['reward_exp']} 经验",
                )
                if achievement.get("level_up"):
                    lines.append(
                        f"🎉 恭喜升级！Lv.{achievement['old_level']} → "
                        f"Lv.{achievement['level']} {achievement['title']}",
                    )
        except Exception:
            self.logger.warning(
                "成就结算失败：\n%s",
                traceback.format_exc(),
            )

        return lines

    async def _run_safely(
        self,
        event: AstrMessageEvent,
        handler: Callable[..., AsyncGenerator[Any, None]],
        *args: Any,
        action: str = "操作",
    ) -> AsyncGenerator[Any, None]:
        """统一异常处理，避免单个错误导致插件崩溃。"""
        try:
            # 记录群会话信息，保证定时公告等后台任务能主动发送消息
            group_id = self._get_group_id(event)
            user_id = str(event.get_sender_id() or "").strip()
            if group_id and user_id:
                self._touch_group_and_user(
                    event,
                    group_id,
                    user_id,
                    event.get_sender_name() or "",
                )
            async for result in handler(event, *args):
                yield result
        except Exception:
            self.logger.error(
                "班级管理插件执行「%s」时出错：\n%s",
                action,
                traceback.format_exc(),
            )
            yield event.plain_result(f"❌ {action}失败，请稍后重试。")
