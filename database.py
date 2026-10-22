"""班级管理插件 v1.5.4 的 SQLite 数据层。

所有数据都通过 ``group_id`` 隔离，每个群（班级）的数据互不影响。
数据库文件默认位于插件目录下的 ``data/class_manager.db``。

历史版本说明：
- v1.1.0：users 表新增经验值（exp）、等级（level）、称号（title）；
- v1.2.0：积分商城商品表（shop_items）与兑换记录表（exchange_logs）；
- v1.3.0：抽奖（含 10 次保底）、每日任务、成就、积分转账、积分明细、
  每日发言计数；
- v1.4.0：群管系统（违禁词、惩罚记录、自动审核配置、警告、公告、白名单）；
- v1.5.0：group_settings 新增 trigger_mode 字段，用于按群保存智能触发方式；
- v1.5.1：新增 get_user_overview，用于个人中心统计兑换/抽奖/违规次数；
- v1.5.3：adjust_points 新增可选 reason 参数，惩罚扣分写入更明确的积分明细；
  数据库结构无变化，惩罚记录继续复用 punish_logs 表；
- v1.5.4：新增 find_users_by_nickname，用于文本 @昵称 与仅含昵称的 At 目标解析。
"""

from __future__ import annotations

import json
import logging
import math
import random
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ----------------------------- 积分规则常量 -----------------------------
DEFAULT_SIGN_REWARD = 1
"""签到基础积分，默认 1 分。"""

CHAT_POINT_PER_MSG = 0.1
"""每条有效群消息获得的积分。"""

CHAT_DAILY_LIMIT = 5.0
"""聊天积分每日上限。"""

CHAT_MIN_INTERVAL_SECONDS = 60
"""同一用户两条计分消息之间的最小间隔（秒）。"""

# ----------------------------- 经验与等级规则 -----------------------------
SIGN_EXP_REWARD = 10
"""每次签到获得的经验值。"""

CHAT_EXP_PER_MSG = 1
"""每条有效群消息获得的经验值。"""

CHAT_EXP_DAILY_LIMIT = 50
"""聊天经验每日上限。"""

DEFAULT_TITLE = "菜鸟学徒"
"""Lv.1 的默认称号。"""

# ----------------------------- 抽奖规则 -----------------------------
LOTTERY_COST = 10
"""每次抽奖消耗的积分。"""

LOTTERY_PITY = 10
"""连续未中奖达到该次数后，下一次抽奖必中（保底）。"""

MAX_LOTTERY_PRIZES = 50
"""单个群最多可配置的奖品数量。"""

# ----------------------------- 转账规则 -----------------------------
TRANSFER_FEE_RATE = 0.05
"""转账手续费比例（5%）。"""

TRANSFER_MIN_FEE = 1
"""转账手续费最低 1 积分。"""

TRANSFER_DAILY_LIMIT = 500
"""每人每天最多转出的积分（按转账本金计算）。"""

TRANSFER_MIN_AGE_DAYS = 7
"""新成员注册满 7 天后才能转账。"""

# ----------------------------- 群管规则常量 -----------------------------
BANNED_PUNISH_ALIASES: dict[str, str] = {
    "警告": "警告",
    "warn": "警告",
    "禁言": "禁言",
    "mute": "禁言",
    "踢出": "踢出",
    "kick": "踢出",
    "积分扣除": "积分扣除",
    "扣分": "积分扣除",
    "deduct": "积分扣除",
}
"""违禁词惩罚类型的中文/英文别名。"""

DEFAULT_BANNED_PUNISH = "警告"
"""添加违禁词时的默认惩罚类型。"""

DEFAULT_BANNED_DURATION = 10
"""违禁词禁言默认时长（分钟），积分扣除时表示默认扣除积分。"""

MAX_MUTE_MINUTES = 43200
"""单次禁言的最大时长（30 天）。"""

DEFAULT_SPAM_THRESHOLD = 5
"""防刷屏默认阈值：次数。"""

DEFAULT_SPAM_WINDOW = 10
"""防刷屏默认窗口：秒数。"""

DEFAULT_SPAM_MUTE_MINUTES = 30
"""触发防刷屏后的默认禁言时长（分钟）。"""

DEFAULT_AD_MUTE_MINUTES = 60
"""触发防广告后的默认禁言时长（分钟）。"""

DEFAULT_NIGHT_MUTE_MINUTES = 10
"""夜间模式下的默认禁言时长（分钟）。"""

AUTO_MOD_FEATURES = ("anti_spam", "anti_ad", "night_mode")
"""自动审核功能标识。"""

AUTO_MOD_LABELS = {
    "anti_spam": "防刷屏",
    "anti_ad": "防广告",
    "night_mode": "夜间模式",
}
"""自动审核功能的中文名称。"""

# ----------------------------- 任务与成就规则 -----------------------------
TASK_TYPE_ALIASES: dict[str, str] = {
    "签到": "sign_in",
    "sign_in": "sign_in",
    "发言": "chat",
    "chat": "chat",
    "连续签到": "streak",
    "streak": "streak",
    "积分": "points",
    "points": "points",
}
"""任务类型的中文/英文别名映射。"""

TASK_TYPE_LABELS: dict[str, str] = {
    "sign_in": "签到",
    "chat": "发言",
    "streak": "连续签到",
    "points": "积分",
}
"""任务类型的中文显示名。"""

TASK_DEFAULT_TARGET: dict[str, int] = {
    "sign_in": 1,
    "chat": 10,
    "streak": 3,
    "points": 100,
}
"""添加任务时未指定目标次数时的默认值。"""

# 默认每日任务：每个群首次使用时会自动写入，管理员可自行增删
DEFAULT_TASKS: list[dict[str, Any]] = [
    {
        "code": "sign_in",
        "name": "每日签到",
        "task_type": "sign_in",
        "target_value": 1,
        "reward_points": 5,
        "reward_exp": 10,
    },
    {
        "code": "chat_10",
        "name": "发言 10 条",
        "task_type": "chat",
        "target_value": 10,
        "reward_points": 3,
        "reward_exp": 5,
    },
    {
        "code": "streak_3",
        "name": "连续签到 3 天",
        "task_type": "streak",
        "target_value": 3,
        "reward_points": 10,
        "reward_exp": 20,
    },
]

# 默认成就定义：全局共享，按群记录用户达成情况
DEFAULT_ACHIEVEMENTS: list[dict[str, Any]] = [
    {
        "code": "first_sign",
        "name": "初来乍到",
        "description": "首次签到",
        "condition_type": "total_signs",
        "target_value": 1,
        "reward_points": 5,
        "reward_exp": 10,
    },
    {
        "code": "streak_7",
        "name": "坚持不懈",
        "description": "连续签到 7 天",
        "condition_type": "streak",
        "target_value": 7,
        "reward_points": 20,
        "reward_exp": 30,
    },
    {
        "code": "chat_50",
        "name": "话痨达人",
        "description": "单日发言 50 条",
        "condition_type": "daily_chat",
        "target_value": 50,
        "reward_points": 10,
        "reward_exp": 20,
    },
    {
        "code": "points_500",
        "name": "积分大亨",
        "description": "积分达到 500",
        "condition_type": "points",
        "target_value": 500,
        "reward_points": 30,
        "reward_exp": 20,
    },
    {
        "code": "level_10",
        "name": "学霸之路",
        "description": "等级达到 Lv.10",
        "condition_type": "level",
        "target_value": 10,
        "reward_points": 50,
        "reward_exp": 50,
    },
    {
        "code": "shop_5",
        "name": "购物狂",
        "description": "累计兑换 5 件商品",
        "condition_type": "exchanges",
        "target_value": 5,
        "reward_points": 20,
        "reward_exp": 30,
    },
    {
        "code": "lucky_1",
        "name": "幸运儿",
        "description": "抽奖中奖 1 次",
        "condition_type": "lottery_wins",
        "target_value": 1,
        "reward_points": 15,
        "reward_exp": 20,
    },
    {
        "code": "tycoon_100",
        "name": "土豪",
        "description": "单次转账超过 100 积分",
        "condition_type": "transfer_amount",
        "target_value": 100,
        "reward_points": 20,
        "reward_exp": 20,
    },
]


def level_for_exp(exp: float) -> int:
    """根据经验值计算等级。

    公式：``level = int(sqrt(exp / 100)) + 1``。
    经验为 0 时等级为 1，经验 100 时等级为 2，经验 400 时等级为 3。
    """
    try:
        value = max(0, int(exp or 0))
    except (TypeError, ValueError):
        value = 0
    return int(math.sqrt(value / 100)) + 1


def title_for_level(level: int) -> str:
    """根据等级返回对应称号。"""
    try:
        level = max(1, int(level or 1))
    except (TypeError, ValueError):
        level = 1

    if level <= 2:
        return "菜鸟学徒"
    if level <= 5:
        return "初级学者"
    if level <= 10:
        return "中级学者"
    if level <= 20:
        return "高级学者"
    if level <= 30:
        return "学霸"
    if level <= 50:
        return "学神"
    return "传奇学帝"


def level_progress(exp: float) -> dict[str, Any]:
    """计算当前等级的升级进度。

    Returns:
        ``level``：当前等级；
        ``title``：当前称号；
        ``current_level_exp``：当前等级所需的起始经验；
        ``next_level_exp``：升到下一级所需的经验；
        ``gained_in_level``：当前等级内已获得的经验；
        ``needed_in_level``：当前等级总共需要的经验；
        ``remaining``：距离下一级还需的经验；
        ``percent``：当前等级内的进度百分比（0~100）。
    """
    try:
        value = max(0, int(exp or 0))
    except (TypeError, ValueError):
        value = 0

    level = level_for_exp(value)
    current_level_exp = 100 * (level - 1) ** 2
    next_level_exp = 100 * level**2
    needed = max(1, next_level_exp - current_level_exp)
    gained = max(0, value - current_level_exp)
    percent = min(100.0, gained / needed * 100)

    return {
        "level": level,
        "title": title_for_level(level),
        "current_level_exp": current_level_exp,
        "next_level_exp": next_level_exp,
        "gained_in_level": gained,
        "needed_in_level": needed,
        "remaining": max(0, next_level_exp - value),
        "percent": percent,
    }


# ----------------------------- 数据表定义 -----------------------------
CORE_SCHEMA = """
-- 用户表：保存每个群内每位成员的数据
CREATE TABLE IF NOT EXISTS users (
    user_id         TEXT    NOT NULL,
    group_id        TEXT    NOT NULL,
    nickname        TEXT    NOT NULL DEFAULT '',
    points          REAL    NOT NULL DEFAULT 0,
    exp             INTEGER NOT NULL DEFAULT 0,
    level           INTEGER NOT NULL DEFAULT 1,
    title           TEXT    NOT NULL DEFAULT '菜鸟学徒',
    total_signs     INTEGER NOT NULL DEFAULT 0,
    streak          INTEGER NOT NULL DEFAULT 0,
    last_sign_date  TEXT,
    last_sign_at    TEXT,
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL,
    PRIMARY KEY (group_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_users_group_points
    ON users(group_id, points DESC);

-- 签到记录表：每天每人最多一条
CREATE TABLE IF NOT EXISTS sign_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT    NOT NULL,
    group_id    TEXT    NOT NULL,
    sign_date   TEXT    NOT NULL,
    sign_at     TEXT    NOT NULL,
    points      REAL    NOT NULL,
    streak      INTEGER NOT NULL DEFAULT 1
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_sign_logs_unique
    ON sign_logs(group_id, user_id, sign_date);

CREATE INDEX IF NOT EXISTS idx_sign_logs_group_date
    ON sign_logs(group_id, sign_date);

-- 聊天积分记录表
CREATE TABLE IF NOT EXISTS chat_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT    NOT NULL,
    group_id    TEXT    NOT NULL,
    message     TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL,
    created_ts  REAL    NOT NULL,
    msg_date    TEXT    NOT NULL,
    points      REAL    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chat_logs_user_day
    ON chat_logs(group_id, user_id, msg_date);

CREATE INDEX IF NOT EXISTS idx_chat_logs_user_ts
    ON chat_logs(group_id, user_id, created_ts DESC);

-- 群设置表：签到奖励、提醒开关等，按群独立保存
CREATE TABLE IF NOT EXISTS group_settings (
    group_id             TEXT PRIMARY KEY,
    unified_msg_origin   TEXT    NOT NULL DEFAULT '',
    platform_id          TEXT    NOT NULL DEFAULT '',
    sign_reward          INTEGER NOT NULL DEFAULT 1,
    reminder_enabled     INTEGER NOT NULL DEFAULT 0,
    last_reminder_date   TEXT,
    defaults_seeded      INTEGER NOT NULL DEFAULT 0,
    trigger_mode         TEXT    NOT NULL DEFAULT '',
    updated_at           TEXT    NOT NULL
);
"""

SHOP_SCHEMA = """
-- 商城商品表：每个群的商品独立保存
CREATE TABLE IF NOT EXISTS shop_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id    TEXT    NOT NULL,
    name        TEXT    NOT NULL,
    cost        INTEGER NOT NULL,
    stock       INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_shop_items_unique
    ON shop_items(group_id, name);

CREATE INDEX IF NOT EXISTS idx_shop_items_group
    ON shop_items(group_id, id);

-- 兑换记录表：保留商品名称快照，商品删除后记录仍然可读
CREATE TABLE IF NOT EXISTS exchange_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id     INTEGER NOT NULL,
    item_name   TEXT    NOT NULL DEFAULT '',
    user_id     TEXT    NOT NULL,
    group_id    TEXT    NOT NULL,
    cost        INTEGER NOT NULL,
    created_at  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_exchange_logs_user
    ON exchange_logs(group_id, user_id, id DESC);

CREATE INDEX IF NOT EXISTS idx_exchange_logs_item
    ON exchange_logs(group_id, item_id);
"""

# ----------------------------- v3.0.0 新增数据表 -----------------------------
V3_SCHEMA = """
-- 抽奖奖品表：每个群独立配置奖池
CREATE TABLE IF NOT EXISTS lottery_prizes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id     TEXT    NOT NULL,
    name         TEXT    NOT NULL,
    probability  REAL    NOT NULL,
    stock        INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT    NOT NULL,
    updated_at   TEXT    NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_lottery_prizes_unique
    ON lottery_prizes(group_id, name);

CREATE INDEX IF NOT EXISTS idx_lottery_prizes_group
    ON lottery_prizes(group_id, id);

-- 抽奖记录表：保留奖品名称快照，奖品删除后记录仍然可读
CREATE TABLE IF NOT EXISTS lottery_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id    TEXT    NOT NULL,
    user_id     TEXT    NOT NULL,
    prize_id    INTEGER,
    prize_name  TEXT    NOT NULL DEFAULT '',
    is_win      INTEGER NOT NULL DEFAULT 0,
    guaranteed  INTEGER NOT NULL DEFAULT 0,
    cost        INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL,
    created_ts  REAL    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_lottery_logs_user
    ON lottery_logs(group_id, user_id, id DESC);

CREATE INDEX IF NOT EXISTS idx_lottery_logs_group
    ON lottery_logs(group_id, id DESC);

-- 每日任务表：每个群可自定义任务
CREATE TABLE IF NOT EXISTS daily_tasks (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id       TEXT    NOT NULL,
    code           TEXT    NOT NULL DEFAULT '',
    name           TEXT    NOT NULL,
    task_type      TEXT    NOT NULL,
    target_value   INTEGER NOT NULL DEFAULT 1,
    reward_points  INTEGER NOT NULL DEFAULT 0,
    reward_exp     INTEGER NOT NULL DEFAULT 0,
    is_default     INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT    NOT NULL
);

-- 默认任务通过 code 去重；自定义任务的 code 为空，不参与唯一约束
CREATE UNIQUE INDEX IF NOT EXISTS idx_daily_tasks_code
    ON daily_tasks(group_id, code) WHERE code <> '';

CREATE INDEX IF NOT EXISTS idx_daily_tasks_group
    ON daily_tasks(group_id, id);

-- 任务完成记录：每个任务每人每天最多完成一次
CREATE TABLE IF NOT EXISTS task_logs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id        INTEGER NOT NULL,
    group_id       TEXT    NOT NULL,
    user_id        TEXT    NOT NULL,
    task_date      TEXT    NOT NULL,
    reward_points  INTEGER NOT NULL DEFAULT 0,
    reward_exp     INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT    NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_task_logs_unique
    ON task_logs(group_id, user_id, task_id, task_date);

-- 成就定义表：全局共享
CREATE TABLE IF NOT EXISTS achievements (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    code            TEXT    NOT NULL UNIQUE,
    name            TEXT    NOT NULL,
    description     TEXT    NOT NULL DEFAULT '',
    condition_type  TEXT    NOT NULL,
    target_value    REAL    NOT NULL DEFAULT 1,
    reward_points   INTEGER NOT NULL DEFAULT 0,
    reward_exp      INTEGER NOT NULL DEFAULT 0
);

-- 用户成就记录表：按群隔离
CREATE TABLE IF NOT EXISTS user_achievements (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    achievement_id  INTEGER NOT NULL,
    group_id        TEXT    NOT NULL,
    user_id         TEXT    NOT NULL,
    achieved_at     TEXT    NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_user_achievements_unique
    ON user_achievements(group_id, user_id, achievement_id);

CREATE INDEX IF NOT EXISTS idx_user_achievements_user
    ON user_achievements(group_id, user_id, id DESC);

-- 转账记录表
CREATE TABLE IF NOT EXISTS transfer_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id    TEXT    NOT NULL,
    from_user   TEXT    NOT NULL,
    to_user     TEXT    NOT NULL,
    amount      REAL    NOT NULL,
    fee         REAL    NOT NULL,
    total       REAL    NOT NULL,
    created_at  TEXT    NOT NULL,
    created_ts  REAL    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_transfer_logs_from
    ON transfer_logs(group_id, from_user, id DESC);

CREATE INDEX IF NOT EXISTS idx_transfer_logs_to
    ON transfer_logs(group_id, to_user, id DESC);

CREATE INDEX IF NOT EXISTS idx_transfer_logs_day
    ON transfer_logs(group_id, from_user, created_ts DESC);

-- 积分变动明细表
CREATE TABLE IF NOT EXISTS points_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id    TEXT    NOT NULL,
    user_id     TEXT    NOT NULL,
    change      REAL    NOT NULL,
    balance     REAL    NOT NULL,
    log_type    TEXT    NOT NULL,
    detail      TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_points_logs_user
    ON points_logs(group_id, user_id, id DESC);

-- 每日发言计数表：用于“发言 N 条”“单日发言 50 条”等条件
CREATE TABLE IF NOT EXISTS daily_stats (
    group_id       TEXT    NOT NULL,
    user_id        TEXT    NOT NULL,
    stat_date      TEXT    NOT NULL,
    message_count  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (group_id, user_id, stat_date)
);
"""

# ----------------------------- v4.0.0 群管数据表 -----------------------------
V4_SCHEMA = """
-- 违禁词表：每个群独立配置
CREATE TABLE IF NOT EXISTS banned_words (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id         TEXT    NOT NULL,
    word             TEXT    NOT NULL,
    added_by         TEXT    NOT NULL DEFAULT '',
    punish_type      TEXT    NOT NULL DEFAULT '警告',
    punish_duration  INTEGER NOT NULL DEFAULT 10,
    created_at       TEXT    NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_banned_words_unique
    ON banned_words(group_id, word COLLATE NOCASE);

CREATE INDEX IF NOT EXISTS idx_banned_words_group
    ON banned_words(group_id, id);

-- 惩罚记录表：自动审核与手动管理都会写入
CREATE TABLE IF NOT EXISTS punish_logs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id     TEXT    NOT NULL,
    user_id      TEXT    NOT NULL,
    punish_type  TEXT    NOT NULL,
    reason       TEXT    NOT NULL DEFAULT '',
    duration     INTEGER NOT NULL DEFAULT 0,
    executor_id  TEXT    NOT NULL DEFAULT '',
    created_at   TEXT    NOT NULL,
    created_ts   REAL    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_punish_logs_user
    ON punish_logs(group_id, user_id, id DESC);

CREATE INDEX IF NOT EXISTS idx_punish_logs_group
    ON punish_logs(group_id, id DESC);

CREATE INDEX IF NOT EXISTS idx_punish_logs_type
    ON punish_logs(group_id, punish_type);

-- 自动审核配置表：每个群每个功能一条记录
CREATE TABLE IF NOT EXISTS auto_mod_config (
    group_id    TEXT    NOT NULL,
    feature     TEXT    NOT NULL,
    enabled     INTEGER NOT NULL DEFAULT 0,
    config      TEXT    NOT NULL DEFAULT '{}',
    updated_at  TEXT    NOT NULL,
    PRIMARY KEY (group_id, feature)
);

-- 警告记录表
CREATE TABLE IF NOT EXISTS warnings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id     TEXT    NOT NULL,
    user_id      TEXT    NOT NULL,
    reason       TEXT    NOT NULL DEFAULT '',
    warned_by    TEXT    NOT NULL DEFAULT '',
    created_at   TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_warnings_user
    ON warnings(group_id, user_id, id DESC);

-- 群公告表：支持立即发布与定时发送
CREATE TABLE IF NOT EXISTS announcements (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id      TEXT    NOT NULL,
    content       TEXT    NOT NULL,
    publisher_id  TEXT    NOT NULL DEFAULT '',
    created_at    TEXT    NOT NULL,
    is_scheduled  INTEGER NOT NULL DEFAULT 0,
    scheduled_at  TEXT,
    sent          INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_announcements_group
    ON announcements(group_id, id DESC);

CREATE INDEX IF NOT EXISTS idx_announcements_due
    ON announcements(is_scheduled, sent, scheduled_at);

-- 群管白名单表：白名单成员不受自动审核限制
CREATE TABLE IF NOT EXISTS whitelist (
    group_id    TEXT    NOT NULL,
    user_id     TEXT    NOT NULL,
    added_by    TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL,
    PRIMARY KEY (group_id, user_id)
);
"""

SCHEMA = CORE_SCHEMA + SHOP_SCHEMA + V3_SCHEMA + V4_SCHEMA


# v1.0.0 升级到 v2.0.0 时需要补充的 users 字段
NEW_USER_COLUMNS: dict[str, str] = {
    "exp": "ALTER TABLE users ADD COLUMN exp INTEGER NOT NULL DEFAULT 0",
    "level": "ALTER TABLE users ADD COLUMN level INTEGER NOT NULL DEFAULT 1",
    "title": ("ALTER TABLE users ADD COLUMN title TEXT NOT NULL DEFAULT '菜鸟学徒'"),
}

# v2.0.0 升级到 v3.0.0 时需要补充的 group_settings 字段
NEW_GROUP_COLUMNS: dict[str, str] = {
    "defaults_seeded": (
        "ALTER TABLE group_settings ADD COLUMN defaults_seeded "
        "INTEGER NOT NULL DEFAULT 0"
    ),
    # v1.5.0 新增：本群触发方式（空字符串表示跟随全局 config.yaml）
    "trigger_mode": (
        "ALTER TABLE group_settings ADD COLUMN trigger_mode "
        "TEXT NOT NULL DEFAULT ''"
    ),
}


class ClassDatabase:
    """班级管理插件的数据库封装。

    使用单个 SQLite 连接，并通过 ``threading.RLock`` 保证线程安全。
    连接设置为自动提交模式，写操作会显式使用 ``BEGIN IMMEDIATE`` 事务。
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._lock = threading.RLock()
        # 已初始化默认任务的群（进程内缓存，避免每条消息重复检查）
        self._seeded_groups: set[str] = set()
        self._conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            isolation_level=None,  # 自动提交，事务由 _tx() 显式控制
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(SCHEMA)
            # 兼容 v1.0.0 / v2.0.0 数据库：自动补齐缺失字段
            self._migrate_schema_locked()
            # 默认成就定义全局共享，启动时确保存在即可
            self._ensure_achievements_locked(self._conn)

    # ------------------------------------------------------------------
    # 基础工具
    # ------------------------------------------------------------------
    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """开启一个写事务，异常时自动回滚。"""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    @staticmethod
    def _now_text() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def close(self) -> None:
        """关闭数据库连接。"""
        with self._lock, suppress(Exception):
            self._conn.close()

    @staticmethod
    def _insert_points_log(
        conn: sqlite3.Connection,
        group_id: str,
        user_id: str,
        change: float,
        balance: float,
        log_type: str,
        detail: str,
        created_at: str,
    ) -> None:
        """在事务内写入一条积分变动明细。

        变动为 0 时不记录，避免产生大量无意义的明细。
        """
        try:
            change_value = round(float(change), 2)
        except (TypeError, ValueError):
            return
        if abs(change_value) < 1e-9:
            return

        conn.execute(
            """
            INSERT INTO points_logs
                (group_id, user_id, change, balance, log_type, detail, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                group_id,
                user_id,
                change_value,
                round(float(balance), 2),
                log_type,
                detail or "",
                created_at,
            ),
        )

    # ------------------------------------------------------------------
    # v3.0.0 默认数据初始化
    # ------------------------------------------------------------------
    def ensure_group_defaults(self, group_id: str) -> None:
        """确保本群已初始化默认每日任务（默认成就在插件启动时初始化）。

        通过 ``group_settings.defaults_seeded`` 标记，保证默认任务只播种一次，
        管理员删除默认任务后不会被自动加回来。
        """
        with self._lock:
            if group_id in self._seeded_groups:
                return

            with self._tx() as conn:
                self._ensure_default_tasks_locked(conn, group_id)
            self._seeded_groups.add(group_id)

    @staticmethod
    def _ensure_achievements_locked(conn: sqlite3.Connection) -> None:
        """写入默认成就定义（全局共享，按 code 去重）。"""
        for item in DEFAULT_ACHIEVEMENTS:
            conn.execute(
                """
                INSERT INTO achievements
                    (code, name, description, condition_type,
                     target_value, reward_points, reward_exp)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(code) DO NOTHING
                """,
                (
                    item["code"],
                    item["name"],
                    item["description"],
                    item["condition_type"],
                    float(item["target_value"]),
                    int(item["reward_points"]),
                    int(item["reward_exp"]),
                ),
            )

    @staticmethod
    def _ensure_default_tasks_locked(
        conn: sqlite3.Connection,
        group_id: str,
    ) -> None:
        """为群写入默认每日任务（每个群只播种一次）。"""
        row = conn.execute(
            "SELECT defaults_seeded FROM group_settings WHERE group_id = ?",
            (group_id,),
        ).fetchone()

        if row is not None and int(row["defaults_seeded"] or 0) == 1:
            return

        now = ClassDatabase._now_text()
        for task in DEFAULT_TASKS:
            conn.execute(
                """
                INSERT OR IGNORE INTO daily_tasks
                    (group_id, code, name, task_type, target_value,
                     reward_points, reward_exp, is_default, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    group_id,
                    task["code"],
                    task["name"],
                    task["task_type"],
                    int(task["target_value"]),
                    int(task["reward_points"]),
                    int(task["reward_exp"]),
                    now,
                ),
            )

        # 标记已播种，同时保证群设置记录存在
        conn.execute(
            """
            INSERT INTO group_settings (group_id, defaults_seeded, updated_at)
            VALUES (?, 1, ?)
            ON CONFLICT(group_id) DO UPDATE SET
                defaults_seeded = 1,
                updated_at = excluded.updated_at
            """,
            (group_id, now),
        )

    # ------------------------------------------------------------------
    # 数据库迁移
    # ------------------------------------------------------------------
    def _migrate_schema_locked(self) -> None:
        """检测并补齐 v2.0.0 新增字段（调用方需已持有锁）。"""
        columns = {
            str(row["name"])
            for row in self._conn.execute("PRAGMA table_info(users)").fetchall()
        }

        added: list[str] = []
        for column, ddl in NEW_USER_COLUMNS.items():
            if column not in columns:
                self._conn.execute(ddl)
                added.append(column)

        # v2.0.0 升级到 v3.0.0：补齐群设置表的新字段
        group_columns = {
            str(row["name"])
            for row in self._conn.execute(
                "PRAGMA table_info(group_settings)",
            ).fetchall()
        }
        for column, ddl in NEW_GROUP_COLUMNS.items():
            if column not in group_columns:
                self._conn.execute(ddl)
                added.append(f"group_settings.{column}")

        # 经验排行榜索引依赖 exp 字段，必须在补列之后创建
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_users_group_exp "
            "ON users(group_id, exp DESC)",
        )

        if added:
            updated = self._recalculate_levels_locked()
            logger.info(
                "数据库迁移完成：新增字段 %s，重算 %s 位用户的等级与称号",
                "、".join(added),
                updated,
            )

    def _recalculate_levels_locked(self) -> int:
        """根据经验值重算所有用户的等级和称号（调用方需已持有锁）。"""
        rows = self._conn.execute(
            "SELECT group_id, user_id, exp, level, title FROM users",
        ).fetchall()

        updates: list[tuple[int, str, str, str]] = []
        for row in rows:
            exp = int(row["exp"] or 0)
            level = level_for_exp(exp)
            title = title_for_level(level)
            if int(row["level"] or 1) != level or str(row["title"] or "") != title:
                updates.append(
                    (level, title, str(row["group_id"]), str(row["user_id"]))
                )

        if updates:
            self._conn.executemany(
                """
                UPDATE users SET level = ?, title = ?
                WHERE group_id = ? AND user_id = ?
                """,
                updates,
            )
        return len(updates)

    def recalculate_levels(self) -> int:
        """公开的等级重算方法，供迁移脚本或维护指令使用。"""
        with self._lock:
            return self._recalculate_levels_locked()

    # ------------------------------------------------------------------
    # 群设置
    # ------------------------------------------------------------------
    def touch_group(
        self,
        group_id: str,
        unified_msg_origin: str = "",
        platform_id: str = "",
    ) -> None:
        """记录群信息，并更新主动消息所需的 unified_msg_origin。"""
        now = self._now_text()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO group_settings
                    (group_id, unified_msg_origin, platform_id, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(group_id) DO UPDATE SET
                    unified_msg_origin = CASE
                        WHEN excluded.unified_msg_origin <> ''
                        THEN excluded.unified_msg_origin
                        ELSE group_settings.unified_msg_origin
                    END,
                    platform_id = CASE
                        WHEN excluded.platform_id <> ''
                        THEN excluded.platform_id
                        ELSE group_settings.platform_id
                    END,
                    updated_at = excluded.updated_at
                """,
                (group_id, unified_msg_origin or "", platform_id or "", now),
            )

    def get_sign_reward(self, group_id: str) -> int:
        """获取本群签到基础积分，未配置时返回默认值。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT sign_reward FROM group_settings WHERE group_id = ?",
                (group_id,),
            ).fetchone()
        if row is None:
            return DEFAULT_SIGN_REWARD
        return int(row["sign_reward"])

    def set_sign_reward(self, group_id: str, value: int) -> None:
        """设置本群签到基础积分。"""
        now = self._now_text()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO group_settings (group_id, sign_reward, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(group_id) DO UPDATE SET
                    sign_reward = excluded.sign_reward,
                    updated_at = excluded.updated_at
                """,
                (group_id, int(value), now),
            )

    def get_group_trigger_mode(self, group_id: str) -> str:
        """获取本群触发方式，未设置时返回空字符串（跟随全局配置）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT trigger_mode FROM group_settings WHERE group_id = ?",
                (group_id,),
            ).fetchone()
        if row is None:
            return ""
        return str(row["trigger_mode"] or "").strip()

    def set_group_trigger_mode(self, group_id: str, mode: str) -> None:
        """设置本群触发方式：all / command / keyword，空字符串表示恢复默认。"""
        now = self._now_text()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO group_settings (group_id, trigger_mode, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(group_id) DO UPDATE SET
                    trigger_mode = excluded.trigger_mode,
                    updated_at = excluded.updated_at
                """,
                (group_id, mode or "", now),
            )

    def set_reminder(
        self,
        group_id: str,
        enabled: bool,
        unified_msg_origin: str = "",
        platform_id: str = "",
    ) -> None:
        """开启或关闭本群的签到提醒。"""
        now = self._now_text()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO group_settings
                    (group_id, unified_msg_origin, platform_id,
                     reminder_enabled, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(group_id) DO UPDATE SET
                    unified_msg_origin = CASE
                        WHEN excluded.unified_msg_origin <> ''
                        THEN excluded.unified_msg_origin
                        ELSE group_settings.unified_msg_origin
                    END,
                    platform_id = CASE
                        WHEN excluded.platform_id <> ''
                        THEN excluded.platform_id
                        ELSE group_settings.platform_id
                    END,
                    reminder_enabled = excluded.reminder_enabled,
                    updated_at = excluded.updated_at
                """,
                (
                    group_id,
                    unified_msg_origin or "",
                    platform_id or "",
                    1 if enabled else 0,
                    now,
                ),
            )

    def list_reminder_groups(self, today: str) -> list[dict[str, Any]]:
        """列出今天尚未发送提醒、且已开启提醒的群。"""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT group_id, unified_msg_origin
                FROM group_settings
                WHERE reminder_enabled = 1
                  AND unified_msg_origin <> ''
                  AND (last_reminder_date IS NULL OR last_reminder_date <> ?)
                """,
                (today,),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_reminder_sent(self, group_id: str, today: str) -> None:
        """标记今天已经给该群发送过提醒。"""
        with self._lock:
            self._conn.execute(
                """
                UPDATE group_settings
                SET last_reminder_date = ?, updated_at = ?
                WHERE group_id = ?
                """,
                (today, self._now_text(), group_id),
            )

    # ------------------------------------------------------------------
    # 用户
    # ------------------------------------------------------------------
    def ensure_user(self, group_id: str, user_id: str, nickname: str = "") -> None:
        """确保用户存在，并尽量更新其群昵称。"""
        now = self._now_text()
        nickname = (nickname or "").strip()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO users
                    (user_id, group_id, nickname, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(group_id, user_id) DO UPDATE SET
                    nickname = CASE
                        WHEN excluded.nickname <> '' THEN excluded.nickname
                        ELSE users.nickname
                    END,
                    updated_at = excluded.updated_at
                """,
                (user_id, group_id, nickname, now, now),
            )

    def get_user(self, group_id: str, user_id: str) -> dict[str, Any] | None:
        """获取用户在指定群的数据。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM users WHERE group_id = ? AND user_id = ?",
                (group_id, user_id),
            ).fetchone()
        return dict(row) if row else None

    def find_users_by_nickname(
        self,
        group_id: str,
        nickname: str,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """按群内昵称精确查找成员，用于 @ 昵称形式的指令。"""
        nickname = (nickname or "").strip()
        if not nickname:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM users WHERE group_id = ? AND nickname = ? LIMIT ?",
                (group_id, nickname, max(1, int(limit))),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_user_overview(self, group_id: str, user_id: str) -> dict[str, int]:
        """v1.5.1：统计个人中心的兑换、抽奖与违规次数。"""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM exchange_logs
                     WHERE group_id = ? AND user_id = ?) AS exchange_count,
                    (SELECT COUNT(*) FROM lottery_logs
                     WHERE group_id = ? AND user_id = ?) AS lottery_count,
                    (SELECT COUNT(*) FROM punish_logs
                     WHERE group_id = ? AND user_id = ?) AS violation_count
                """,
                (group_id, user_id, group_id, user_id, group_id, user_id),
            ).fetchone()
        if row is None:
            return {
                "exchange_count": 0,
                "lottery_count": 0,
                "violation_count": 0,
            }
        return {
            "exchange_count": int(row["exchange_count"] or 0),
            "lottery_count": int(row["lottery_count"] or 0),
            "violation_count": int(row["violation_count"] or 0),
        }

    def get_rank(self, group_id: str, points: float) -> int:
        """按积分计算排名（积分相同则名次相同）。"""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT COUNT(*) AS cnt
                FROM users
                WHERE group_id = ? AND points > ?
                """,
                (group_id, round(float(points), 2)),
            ).fetchone()
        return int(row["cnt"]) + 1 if row else 1

    def get_leaderboard(self, group_id: str, limit: int = 10) -> list[dict[str, Any]]:
        """获取本群积分排行榜（仅包含积分大于 0 的成员）。"""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT user_id, nickname, points, total_signs, exp, level, title
                FROM users
                WHERE group_id = ? AND points > 0
                ORDER BY points DESC, total_signs DESC, user_id ASC
                LIMIT ?
                """,
                (group_id, int(limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_unsigned_users(
        self,
        group_id: str,
        today: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """获取今天还没有签到的已登记成员。"""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT user_id, nickname, points
                FROM users
                WHERE group_id = ?
                  AND (last_sign_date IS NULL OR last_sign_date <> ?)
                ORDER BY points DESC, user_id ASC
                LIMIT ?
                """,
                (group_id, today, int(limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # 签到
    # ------------------------------------------------------------------
    def sign_in(
        self,
        group_id: str,
        user_id: str,
        nickname: str = "",
        base_reward: int = DEFAULT_SIGN_REWARD,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """执行签到。

        积分规则：
        - 每天限签一次，获得基础积分；
        - 连续签到天数每满 7 天额外 +3 分，每满 30 天额外 +10 分。

        经验规则：
        - 每次签到固定获得 10 点经验；
        - 升级后自动更新等级和称号。

        Returns:
            包含签到结果的字典，字段见下方注释。
        """
        now = now or datetime.now()
        today = now.strftime("%Y-%m-%d")
        yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        now_text = now.strftime("%Y-%m-%d %H:%M:%S")
        nickname = (nickname or "").strip()
        base_reward = max(0, int(base_reward))

        with self._tx() as conn:
            # 确保用户记录存在
            conn.execute(
                """
                INSERT INTO users
                    (user_id, group_id, nickname, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(group_id, user_id) DO UPDATE SET
                    nickname = CASE
                        WHEN excluded.nickname <> '' THEN excluded.nickname
                        ELSE users.nickname
                    END,
                    updated_at = excluded.updated_at
                """,
                (user_id, group_id, nickname, now_text, now_text),
            )

            row = conn.execute(
                "SELECT * FROM users WHERE group_id = ? AND user_id = ?",
                (group_id, user_id),
            ).fetchone()
            if row is None:  # 理论上不会发生，做一层兜底
                raise RuntimeError("用户记录创建失败")

            old_points = round(float(row["points"]), 2)
            old_streak = int(row["streak"] or 0)
            old_exp = int(row["exp"] or 0)
            old_level = int(row["level"] or 1)
            old_title = str(row["title"] or title_for_level(old_level))

            # 今天已经签过到
            if row["last_sign_date"] == today:
                return {
                    "already_signed": True,
                    "points": old_points,
                    "gained": 0.0,
                    "base_reward": base_reward,
                    "bonus": 0,
                    "bonus_details": [],
                    "streak": old_streak,
                    "total_signs": int(row["total_signs"] or 0),
                    "exp": old_exp,
                    "exp_gained": 0,
                    "level": old_level,
                    "title": old_title,
                    "old_level": old_level,
                    "old_title": old_title,
                    "level_up": False,
                }

            if row["last_sign_date"] == yesterday:
                streak = old_streak + 1
            else:
                streak = 1

            # 计算连续签到额外奖励
            bonus_details: list[tuple[str, int]] = []
            bonus = 0
            if streak > 0 and streak % 7 == 0:
                bonus_details.append(("连续签到 7 天", 3))
                bonus += 3
            if streak > 0 and streak % 30 == 0:
                bonus_details.append(("连续签到 30 天", 10))
                bonus += 10

            gained = round(base_reward + bonus, 2)
            new_points = round(old_points + gained, 2)
            total_signs = int(row["total_signs"] or 0) + 1

            # 签到获得经验，并重新计算等级与称号
            new_exp = old_exp + SIGN_EXP_REWARD
            new_level = level_for_exp(new_exp)
            new_title = title_for_level(new_level)
            level_up = new_level > old_level

            conn.execute(
                """
                UPDATE users
                SET points = ?, total_signs = ?, streak = ?,
                    last_sign_date = ?, last_sign_at = ?,
                    exp = ?, level = ?, title = ?, updated_at = ?
                WHERE group_id = ? AND user_id = ?
                """,
                (
                    new_points,
                    total_signs,
                    streak,
                    today,
                    now_text,
                    new_exp,
                    new_level,
                    new_title,
                    now_text,
                    group_id,
                    user_id,
                ),
            )
            conn.execute(
                """
                INSERT INTO sign_logs
                    (user_id, group_id, sign_date, sign_at, points, streak)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (user_id, group_id, today, now_text, gained, streak),
            )

            # 写入积分变动明细（v3.0.0）
            self._insert_points_log(
                conn,
                group_id,
                user_id,
                gained,
                new_points,
                "签到",
                f"每日签到（连续 {streak} 天）",
                now_text,
            )

        return {
            "already_signed": False,
            "points": new_points,
            "gained": gained,
            "base_reward": base_reward,
            "bonus": bonus,
            "bonus_details": bonus_details,
            "streak": streak,
            "total_signs": total_signs,
            "exp": new_exp,
            "exp_gained": SIGN_EXP_REWARD,
            "level": new_level,
            "title": new_title,
            "old_level": old_level,
            "old_title": old_title,
            "level_up": level_up,
        }

    # ------------------------------------------------------------------
    # 积分管理
    # ------------------------------------------------------------------
    def reset_points(self, group_id: str, user_id: str, nickname: str = "") -> float:
        """将指定用户的积分清零，返回清零前的积分。"""
        now = self._now_text()
        nickname = (nickname or "").strip()
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO users
                    (user_id, group_id, nickname, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(group_id, user_id) DO UPDATE SET
                    nickname = CASE
                        WHEN excluded.nickname <> '' THEN excluded.nickname
                        ELSE users.nickname
                    END,
                    updated_at = excluded.updated_at
                """,
                (user_id, group_id, nickname, now, now),
            )
            row = conn.execute(
                "SELECT points FROM users WHERE group_id = ? AND user_id = ?",
                (group_id, user_id),
            ).fetchone()
            old_points = round(float(row["points"]), 2) if row else 0.0
            conn.execute(
                """
                UPDATE users SET points = 0, updated_at = ?
                WHERE group_id = ? AND user_id = ?
                """,
                (now, group_id, user_id),
            )
            self._insert_points_log(
                conn,
                group_id,
                user_id,
                -old_points,
                0.0,
                "管理",
                "管理员清零积分",
                now,
            )
        return old_points

    def adjust_points(
        self,
        group_id: str,
        user_id: str,
        delta: float,
        nickname: str = "",
        reason: str = "",
    ) -> dict[str, float]:
        """增加或减少积分，最低为 0。

        Args:
            reason: 可选，写入积分明细的说明（如“积分惩罚：刷屏”）。

        Returns:
            ``{"old": 旧积分, "new": 新积分, "applied": 实际变化量}``
        """
        now = self._now_text()
        nickname = (nickname or "").strip()
        delta = round(float(delta), 2)
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO users
                    (user_id, group_id, nickname, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(group_id, user_id) DO UPDATE SET
                    nickname = CASE
                        WHEN excluded.nickname <> '' THEN excluded.nickname
                        ELSE users.nickname
                    END,
                    updated_at = excluded.updated_at
                """,
                (user_id, group_id, nickname, now, now),
            )
            row = conn.execute(
                "SELECT points FROM users WHERE group_id = ? AND user_id = ?",
                (group_id, user_id),
            ).fetchone()
            old_points = round(float(row["points"]), 2) if row else 0.0
            new_points = max(0.0, round(old_points + delta, 2))
            applied = round(new_points - old_points, 2)

            conn.execute(
                """
                UPDATE users SET points = ?, updated_at = ?
                WHERE group_id = ? AND user_id = ?
                """,
                (new_points, now, group_id, user_id),
            )
            self._insert_points_log(
                conn,
                group_id,
                user_id,
                applied,
                new_points,
                "管理",
                reason or ("管理员增加积分" if delta > 0 else "管理员减少积分"),
                now,
            )
        return {"old": old_points, "new": new_points, "applied": applied}

    # ------------------------------------------------------------------
    # 聊天积分与经验
    # ------------------------------------------------------------------
    def add_chat_points(
        self,
        group_id: str,
        user_id: str,
        message: str,
        nickname: str = "",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """记录一条群消息，并按规则发放聊天积分与经验。

        积分规则：
        - 每条有效消息 +0.1 分；
        - 每天每人最多 5 分；
        - 同一用户 1 分钟内最多计 1 条。

        经验规则：
        - 每条计分消息 +1 经验；
        - 每天最多通过聊天获得 50 经验。

        Returns:
            包含积分、经验与升级信息的字典。
        """
        now = now or datetime.now()
        today = now.strftime("%Y-%m-%d")
        now_text = now.strftime("%Y-%m-%d %H:%M:%S")
        now_ts = now.timestamp()
        nickname = (nickname or "").strip()
        message = (message or "").strip()[:200]

        with self._tx() as conn:
            # 确保用户记录存在
            conn.execute(
                """
                INSERT INTO users
                    (user_id, group_id, nickname, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(group_id, user_id) DO UPDATE SET
                    nickname = CASE
                        WHEN excluded.nickname <> '' THEN excluded.nickname
                        ELSE users.nickname
                    END,
                    updated_at = excluded.updated_at
                """,
                (user_id, group_id, nickname, now_text, now_text),
            )

            # 统计今日发言条数（用于“发言 N 条”任务与“单日发言 50 条”成就）
            conn.execute(
                """
                INSERT INTO daily_stats
                    (group_id, user_id, stat_date, message_count)
                VALUES (?, ?, ?, 1)
                ON CONFLICT(group_id, user_id, stat_date) DO UPDATE SET
                    message_count = daily_stats.message_count + 1
                """,
                (group_id, user_id, today),
            )

            user_row = conn.execute(
                """
                SELECT points, exp, level, title
                FROM users
                WHERE group_id = ? AND user_id = ?
                """,
                (group_id, user_id),
            ).fetchone()
            old_points = round(float(user_row["points"]), 2) if user_row else 0.0
            old_exp = int(user_row["exp"] or 0) if user_row else 0
            old_level = int(user_row["level"] or 1) if user_row else 1
            old_title = (
                str(user_row["title"] or title_for_level(old_level))
                if user_row
                else DEFAULT_TITLE
            )

            # 今日已获得的聊天积分
            row = conn.execute(
                """
                SELECT COALESCE(SUM(points), 0) AS total
                FROM chat_logs
                WHERE group_id = ? AND user_id = ? AND msg_date = ?
                """,
                (group_id, user_id, today),
            ).fetchone()
            today_points = round(float(row["total"]), 2)

            if today_points >= CHAT_DAILY_LIMIT:
                return {
                    "awarded": False,
                    "reason": "daily_limit",
                    "points": 0.0,
                    "total": old_points,
                    "exp_gained": 0,
                    "exp": old_exp,
                    "level": old_level,
                    "title": old_title,
                    "old_level": old_level,
                    "old_title": old_title,
                    "level_up": False,
                }

            # 同一用户 1 分钟内最多计 1 条
            last_row = conn.execute(
                """
                SELECT created_ts
                FROM chat_logs
                WHERE group_id = ? AND user_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (group_id, user_id),
            ).fetchone()
            if last_row is not None:
                elapsed = now_ts - float(last_row["created_ts"])
                if elapsed < CHAT_MIN_INTERVAL_SECONDS:
                    return {
                        "awarded": False,
                        "reason": "interval",
                        "points": 0.0,
                        "total": old_points,
                        "exp_gained": 0,
                        "exp": old_exp,
                        "level": old_level,
                        "title": old_title,
                        "old_level": old_level,
                        "old_title": old_title,
                        "level_up": False,
                    }

            # 正常情况每次 +0.1；接近上限时按剩余额度发放
            awarded = round(
                min(CHAT_POINT_PER_MSG, CHAT_DAILY_LIMIT - today_points),
                2,
            )
            if awarded <= 0:
                return {
                    "awarded": False,
                    "reason": "daily_limit",
                    "points": 0.0,
                    "total": old_points,
                    "exp_gained": 0,
                    "exp": old_exp,
                    "level": old_level,
                    "title": old_title,
                    "old_level": old_level,
                    "old_title": old_title,
                    "level_up": False,
                }

            # 今日已获得经验的计分消息数量（每条计分消息固定 +1 经验）
            exp_row = conn.execute(
                """
                SELECT COUNT(*) AS cnt
                FROM chat_logs
                WHERE group_id = ? AND user_id = ? AND msg_date = ?
                """,
                (group_id, user_id, today),
            ).fetchone()
            today_exp_messages = int(exp_row["cnt"]) if exp_row else 0
            exp_gained = (
                CHAT_EXP_PER_MSG if today_exp_messages < CHAT_EXP_DAILY_LIMIT else 0
            )

            conn.execute(
                """
                INSERT INTO chat_logs
                    (user_id, group_id, message, created_at, created_ts,
                     msg_date, points)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    group_id,
                    message,
                    now_text,
                    now_ts,
                    today,
                    awarded,
                ),
            )

            new_points = round(old_points + awarded, 2)
            new_exp = old_exp + exp_gained
            new_level = level_for_exp(new_exp) if exp_gained else old_level
            new_title = title_for_level(new_level)
            level_up = new_level > old_level

            conn.execute(
                """
                UPDATE users
                SET points = ?, exp = ?, level = ?, title = ?, updated_at = ?
                WHERE group_id = ? AND user_id = ?
                """,
                (
                    new_points,
                    new_exp,
                    new_level,
                    new_title,
                    now_text,
                    group_id,
                    user_id,
                ),
            )
            # 写入积分变动明细（v3.0.0）
            self._insert_points_log(
                conn,
                group_id,
                user_id,
                awarded,
                new_points,
                "聊天",
                "群聊发言",
                now_text,
            )

        return {
            "awarded": True,
            "reason": "ok",
            "points": awarded,
            "total": new_points,
            "exp_gained": exp_gained,
            "exp": new_exp,
            "level": new_level,
            "title": new_title,
            "old_level": old_level,
            "old_title": old_title,
            "level_up": level_up,
        }

    # ------------------------------------------------------------------
    # 积分商城：商品管理
    # ------------------------------------------------------------------
    def add_shop_item(
        self,
        group_id: str,
        name: str,
        cost: int,
        stock: int,
    ) -> dict[str, Any]:
        """添加一个商城商品。

        Returns:
            ``{"ok": True, ...商品信息}`` 或
            ``{"ok": False, "reason": "duplicate", "item_id": 已有商品ID}``。
        """
        name = (name or "").strip()
        cost = int(cost)
        stock = int(stock)
        now = self._now_text()

        with self._tx() as conn:
            existing = conn.execute(
                """
                SELECT id FROM shop_items
                WHERE group_id = ? AND name = ?
                """,
                (group_id, name),
            ).fetchone()
            if existing is not None:
                return {
                    "ok": False,
                    "reason": "duplicate",
                    "item_id": int(existing["id"]),
                }

            cursor = conn.execute(
                """
                INSERT INTO shop_items
                    (group_id, name, cost, stock, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (group_id, name, cost, stock, now, now),
            )
            item_id = int(cursor.lastrowid or 0)

        return {
            "ok": True,
            "item_id": item_id,
            "name": name,
            "cost": cost,
            "stock": stock,
        }

    def delete_shop_item(self, group_id: str, item_id: int) -> bool:
        """删除指定商品，返回是否删除成功（兑换记录会保留）。"""
        with self._tx() as conn:
            cursor = conn.execute(
                """
                DELETE FROM shop_items
                WHERE group_id = ? AND id = ?
                """,
                (group_id, int(item_id)),
            )
            return cursor.rowcount > 0

    def list_shop_items(self, group_id: str) -> list[dict[str, Any]]:
        """列出本群全部商品，有库存的排在前面。"""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, name, cost, stock, created_at, updated_at
                FROM shop_items
                WHERE group_id = ?
                ORDER BY CASE WHEN stock > 0 THEN 0 ELSE 1 END, id ASC
                """,
                (group_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_shop_item(self, group_id: str, item_id: int) -> dict[str, Any] | None:
        """获取单个商品信息。"""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT id, name, cost, stock
                FROM shop_items
                WHERE group_id = ? AND id = ?
                """,
                (group_id, int(item_id)),
            ).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # 积分商城：兑换
    # ------------------------------------------------------------------
    def exchange_item(
        self,
        group_id: str,
        user_id: str,
        item_id: int,
        nickname: str = "",
    ) -> dict[str, Any]:
        """兑换商品：原子地检查积分与库存、扣减积分、扣减库存并写入记录。

        Returns:
            ``status`` 可能为：
            - ``ok``：兑换成功；
            - ``not_found``：商品不存在；
            - ``sold_out``：库存不足；
            - ``insufficient``：积分不足。
        """
        now = self._now_text()
        nickname = (nickname or "").strip()

        with self._tx() as conn:
            # 确保用户记录存在
            conn.execute(
                """
                INSERT INTO users
                    (user_id, group_id, nickname, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(group_id, user_id) DO UPDATE SET
                    nickname = CASE
                        WHEN excluded.nickname <> '' THEN excluded.nickname
                        ELSE users.nickname
                    END,
                    updated_at = excluded.updated_at
                """,
                (user_id, group_id, nickname, now, now),
            )

            item = conn.execute(
                """
                SELECT id, name, cost, stock
                FROM shop_items
                WHERE group_id = ? AND id = ?
                """,
                (group_id, int(item_id)),
            ).fetchone()
            if item is None:
                return {"status": "not_found"}

            cost = int(item["cost"])
            stock = int(item["stock"])
            if stock <= 0:
                return {
                    "status": "sold_out",
                    "item_name": str(item["name"]),
                }

            user = conn.execute(
                """
                SELECT points FROM users
                WHERE group_id = ? AND user_id = ?
                """,
                (group_id, user_id),
            ).fetchone()
            points = round(float(user["points"]), 2) if user else 0.0
            if points < cost:
                return {
                    "status": "insufficient",
                    "item_name": str(item["name"]),
                    "points": points,
                    "cost": cost,
                }

            new_points = round(points - cost, 2)
            conn.execute(
                """
                UPDATE users SET points = ?, updated_at = ?
                WHERE group_id = ? AND user_id = ?
                """,
                (new_points, now, group_id, user_id),
            )

            cursor = conn.execute(
                """
                UPDATE shop_items
                SET stock = stock - 1, updated_at = ?
                WHERE group_id = ? AND id = ? AND stock > 0
                """,
                (now, group_id, int(item_id)),
            )
            if cursor.rowcount <= 0:  # 理论上不会发生，事务内库存已被锁定
                raise RuntimeError("商品库存扣减失败")

            log_cursor = conn.execute(
                """
                INSERT INTO exchange_logs
                    (item_id, item_name, user_id, group_id, cost, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    int(item["id"]),
                    str(item["name"]),
                    user_id,
                    group_id,
                    cost,
                    now,
                ),
            )
            self._insert_points_log(
                conn,
                group_id,
                user_id,
                -cost,
                new_points,
                "兑换",
                f"兑换「{item['name']}」",
                now,
            )

        return {
            "status": "ok",
            "exchange_id": int(log_cursor.lastrowid or 0),
            "item_id": int(item["id"]),
            "item_name": str(item["name"]),
            "cost": cost,
            "points": new_points,
            "remaining_stock": stock - 1,
        }

    def get_exchange_logs(
        self,
        group_id: str,
        user_id: str,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """获取指定用户在群内的兑换记录（按时间倒序）。"""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, item_id, item_name, cost, created_at
                FROM exchange_logs
                WHERE group_id = ? AND user_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (group_id, user_id, int(limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_shop_overview(self, group_id: str) -> list[dict[str, Any]]:
        """获取商城管理视图：商品及兑换统计。"""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT
                    s.id,
                    s.name,
                    s.cost,
                    s.stock,
                    COUNT(e.id) AS exchange_count,
                    COALESCE(SUM(e.cost), 0) AS total_cost,
                    MAX(e.created_at) AS last_exchange_at
                FROM shop_items AS s
                LEFT JOIN exchange_logs AS e
                    ON e.group_id = s.group_id AND e.item_id = s.id
                WHERE s.group_id = ?
                GROUP BY s.id
                ORDER BY s.id ASC
                """,
                (group_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    # ==================================================================
    # v3.0.0 抽奖系统
    # ==================================================================
    def add_lottery_prize(
        self,
        group_id: str,
        name: str,
        probability: float,
        stock: int,
    ) -> dict[str, Any]:
        """添加一个抽奖奖品。

        规则：
        - 同一群内奖品名称不能重复；
        - 中奖概率必须大于 0，且所有奖品概率之和不能超过 100%；
        - 单个群最多 50 个奖品。

        Returns:
            ``{"ok": True, ...}`` 或带 ``reason`` 的失败结果：
            ``duplicate``（重名）、``too_many``（数量超限）、
            ``probability``（概率总和超限）。
        """
        name = (name or "").strip()
        try:
            probability = round(float(probability), 2)
            stock = int(stock)
        except (TypeError, ValueError):
            return {"ok": False, "reason": "invalid"}

        now = self._now_text()
        with self._tx() as conn:
            existing = conn.execute(
                """
                SELECT id FROM lottery_prizes
                WHERE group_id = ? AND name = ?
                """,
                (group_id, name),
            ).fetchone()
            if existing is not None:
                return {
                    "ok": False,
                    "reason": "duplicate",
                    "prize_id": int(existing["id"]),
                }

            count_row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM lottery_prizes WHERE group_id = ?",
                (group_id,),
            ).fetchone()
            if int(count_row["cnt"]) >= MAX_LOTTERY_PRIZES:
                return {"ok": False, "reason": "too_many"}

            sum_row = conn.execute(
                """
                SELECT COALESCE(SUM(probability), 0) AS total
                FROM lottery_prizes
                WHERE group_id = ?
                """,
                (group_id,),
            ).fetchone()
            total = round(float(sum_row["total"]), 2)
            if round(total + probability, 2) > 100.0 + 1e-9:
                return {
                    "ok": False,
                    "reason": "probability",
                    "total": total,
                    "remaining": round(100.0 - total, 2),
                }

            cursor = conn.execute(
                """
                INSERT INTO lottery_prizes
                    (group_id, name, probability, stock, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (group_id, name, probability, stock, now, now),
            )
            prize_id = int(cursor.lastrowid or 0)

        return {
            "ok": True,
            "prize_id": prize_id,
            "name": name,
            "probability": probability,
            "stock": stock,
            "probability_total": round(total + probability, 2),
        }

    def delete_lottery_prize(self, group_id: str, prize_id: int) -> bool:
        """删除指定奖品，返回是否删除成功（历史抽奖记录会保留）。"""
        with self._tx() as conn:
            cursor = conn.execute(
                """
                DELETE FROM lottery_prizes
                WHERE group_id = ? AND id = ?
                """,
                (group_id, int(prize_id)),
            )
            return cursor.rowcount > 0

    def get_lottery_prize(
        self,
        group_id: str,
        prize_id: int,
    ) -> dict[str, Any] | None:
        """获取单个奖品信息。"""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT id, name, probability, stock
                FROM lottery_prizes
                WHERE group_id = ? AND id = ?
                """,
                (group_id, int(prize_id)),
            ).fetchone()
        return dict(row) if row else None

    def list_lottery_prizes(self, group_id: str) -> list[dict[str, Any]]:
        """列出本群全部奖品，有库存的排在前面。"""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, name, probability, stock, created_at, updated_at
                FROM lottery_prizes
                WHERE group_id = ?
                ORDER BY CASE WHEN stock > 0 THEN 0 ELSE 1 END, id ASC
                """,
                (group_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def draw_lottery(
        self,
        group_id: str,
        user_id: str,
        nickname: str = "",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """执行一次抽奖（扣除 10 积分，从奖池随机抽取）。

        保底机制：同一用户连续 10 次未中奖后，第 11 次必定中奖，
        此时选择库存中中奖概率最低的奖品（视为“最低奖”）。

        Returns:
            ``status`` 可能为：
            - ``ok``：抽奖完成（``win`` 表示是否中奖）；
            - ``insufficient``：积分不足；
            - ``no_prize``：奖池中没有有库存的奖品（不扣积分）。
        """
        now = now or datetime.now()
        now_text = now.strftime("%Y-%m-%d %H:%M:%S")
        now_ts = now.timestamp()
        nickname = (nickname or "").strip()

        with self._tx() as conn:
            # 确保用户记录存在
            conn.execute(
                """
                INSERT INTO users
                    (user_id, group_id, nickname, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(group_id, user_id) DO UPDATE SET
                    nickname = CASE
                        WHEN excluded.nickname <> '' THEN excluded.nickname
                        ELSE users.nickname
                    END,
                    updated_at = excluded.updated_at
                """,
                (user_id, group_id, nickname, now_text, now_text),
            )

            user_row = conn.execute(
                """
                SELECT points FROM users
                WHERE group_id = ? AND user_id = ?
                """,
                (group_id, user_id),
            ).fetchone()
            points = round(float(user_row["points"]), 2) if user_row else 0.0
            if points < LOTTERY_COST:
                return {
                    "status": "insufficient",
                    "points": points,
                    "cost": LOTTERY_COST,
                }

            prizes = conn.execute(
                """
                SELECT id, name, probability, stock
                FROM lottery_prizes
                WHERE group_id = ? AND stock > 0
                ORDER BY id ASC
                """,
                (group_id,),
            ).fetchall()
            if not prizes:
                return {"status": "no_prize", "points": points}

            # 计算连续未中奖次数：最后一次中奖之后的所有抽奖记录
            last_win_row = conn.execute(
                """
                SELECT COALESCE(MAX(id), 0) AS last_win_id
                FROM lottery_logs
                WHERE group_id = ? AND user_id = ? AND is_win = 1
                """,
                (group_id, user_id),
            ).fetchone()
            last_win_id = int(last_win_row["last_win_id"] or 0)
            miss_row = conn.execute(
                """
                SELECT COUNT(*) AS cnt
                FROM lottery_logs
                WHERE group_id = ? AND user_id = ? AND id > ?
                """,
                (group_id, user_id, last_win_id),
            ).fetchone()
            miss_streak = int(miss_row["cnt"]) if miss_row else 0

            guaranteed = miss_streak >= LOTTERY_PITY
            target = None
            if guaranteed:
                # 保底：选择概率最低（并列时编号最小）的有库存奖品
                target = min(
                    prizes,
                    key=lambda row: (float(row["probability"]), int(row["id"])),
                )
            else:
                roll = random.random() * 100.0
                cumulative = 0.0
                for prize in prizes:
                    cumulative += float(prize["probability"])
                    if roll < cumulative:
                        target = prize
                        break

            new_points = round(points - LOTTERY_COST, 2)
            conn.execute(
                """
                UPDATE users SET points = ?, updated_at = ?
                WHERE group_id = ? AND user_id = ?
                """,
                (new_points, now_text, group_id, user_id),
            )

            if target is not None:
                conn.execute(
                    """
                    UPDATE lottery_prizes
                    SET stock = stock - 1, updated_at = ?
                    WHERE group_id = ? AND id = ? AND stock > 0
                    """,
                    (now_text, group_id, int(target["id"])),
                )
                conn.execute(
                    """
                    INSERT INTO lottery_logs
                        (group_id, user_id, prize_id, prize_name, is_win,
                         guaranteed, cost, created_at, created_ts)
                    VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?)
                    """,
                    (
                        group_id,
                        user_id,
                        int(target["id"]),
                        str(target["name"]),
                        1 if guaranteed else 0,
                        LOTTERY_COST,
                        now_text,
                        now_ts,
                    ),
                )
                self._insert_points_log(
                    conn,
                    group_id,
                    user_id,
                    -LOTTERY_COST,
                    new_points,
                    "抽奖",
                    f"抽奖获得「{target['name']}」",
                    now_text,
                )
                result = {
                    "status": "ok",
                    "win": True,
                    "guaranteed": guaranteed,
                    "prize_id": int(target["id"]),
                    "prize_name": str(target["name"]),
                    "remaining_stock": int(target["stock"]) - 1,
                    "miss_streak": 0,
                    "next_miss_streak": 0,
                }
            else:
                conn.execute(
                    """
                    INSERT INTO lottery_logs
                        (group_id, user_id, prize_id, prize_name, is_win,
                         guaranteed, cost, created_at, created_ts)
                    VALUES (?, ?, NULL, '', 0, 0, ?, ?, ?)
                    """,
                    (group_id, user_id, LOTTERY_COST, now_text, now_ts),
                )
                self._insert_points_log(
                    conn,
                    group_id,
                    user_id,
                    -LOTTERY_COST,
                    new_points,
                    "抽奖",
                    "抽奖未中奖",
                    now_text,
                )
                result = {
                    "status": "ok",
                    "win": False,
                    "guaranteed": False,
                    "prize_name": "",
                    "miss_streak": miss_streak + 1,
                    "next_miss_streak": max(0, LOTTERY_PITY - miss_streak - 1),
                }

        result["cost"] = LOTTERY_COST
        result["points"] = new_points
        return result

    def get_lottery_logs(
        self,
        group_id: str,
        user_id: str,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """获取个人抽奖历史（按时间倒序）。"""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, prize_id, prize_name, is_win, guaranteed,
                       cost, created_at
                FROM lottery_logs
                WHERE group_id = ? AND user_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (group_id, user_id, int(limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_lottery_stats(self, group_id: str) -> dict[str, Any]:
        """获取本群抽奖统计数据（管理员查看）。"""
        with self._lock:
            summary = self._conn.execute(
                """
                SELECT
                    COUNT(*) AS total_draws,
                    COALESCE(SUM(CASE WHEN is_win = 1 THEN 1 ELSE 0 END), 0)
                        AS total_wins,
                    COALESCE(SUM(cost), 0) AS total_cost,
                    COUNT(DISTINCT user_id) AS user_count
                FROM lottery_logs
                WHERE group_id = ?
                """,
                (group_id,),
            ).fetchone()
            prize_rows = self._conn.execute(
                """
                SELECT prize_name, COUNT(*) AS wins
                FROM lottery_logs
                WHERE group_id = ? AND is_win = 1
                GROUP BY prize_name
                ORDER BY wins DESC, prize_name ASC
                """,
                (group_id,),
            ).fetchall()

        stats = dict(summary) if summary else {}
        stats["prizes"] = [dict(row) for row in prize_rows]
        total_draws = int(stats.get("total_draws", 0) or 0)
        total_wins = int(stats.get("total_wins", 0) or 0)
        stats["win_rate"] = (
            round(total_wins / total_draws * 100, 2) if total_draws else 0.0
        )
        return stats

    # ==================================================================
    # v3.0.0 每日任务系统
    # ==================================================================
    @staticmethod
    def _task_progress_locked(
        conn: sqlite3.Connection,
        group_id: str,
        user_id: str,
        task_type: str,
        today: str,
    ) -> float:
        """计算某个任务类型在当前用户身上的进度（调用方需持有事务）。"""
        if task_type == "sign_in":
            row = conn.execute(
                """
                SELECT last_sign_date FROM users
                WHERE group_id = ? AND user_id = ?
                """,
                (group_id, user_id),
            ).fetchone()
            return 1.0 if row and row["last_sign_date"] == today else 0.0

        if task_type == "chat":
            row = conn.execute(
                """
                SELECT message_count FROM daily_stats
                WHERE group_id = ? AND user_id = ? AND stat_date = ?
                """,
                (group_id, user_id, today),
            ).fetchone()
            return float(row["message_count"]) if row else 0.0

        if task_type == "streak":
            row = conn.execute(
                """
                SELECT streak FROM users
                WHERE group_id = ? AND user_id = ?
                """,
                (group_id, user_id),
            ).fetchone()
            return float(row["streak"] or 0) if row else 0.0

        if task_type == "points":
            row = conn.execute(
                """
                SELECT points FROM users
                WHERE group_id = ? AND user_id = ?
                """,
                (group_id, user_id),
            ).fetchone()
            return float(row["points"] or 0) if row else 0.0

        return 0.0

    def list_tasks(self, group_id: str) -> list[dict[str, Any]]:
        """列出本群全部每日任务（不计算个人进度）。"""
        self.ensure_group_defaults(group_id)
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, name, task_type, target_value,
                       reward_points, reward_exp, is_default, created_at
                FROM daily_tasks
                WHERE group_id = ?
                ORDER BY id ASC
                """,
                (group_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_tasks_with_progress(
        self,
        group_id: str,
        user_id: str,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """列出本群任务，并附带指定用户今日的完成进度。"""
        self.ensure_group_defaults(group_id)
        now = now or datetime.now()
        today = now.strftime("%Y-%m-%d")

        with self._lock:
            tasks = self._conn.execute(
                """
                SELECT id, name, task_type, target_value,
                       reward_points, reward_exp, is_default
                FROM daily_tasks
                WHERE group_id = ?
                ORDER BY id ASC
                """,
                (group_id,),
            ).fetchall()
            done_rows = self._conn.execute(
                """
                SELECT task_id FROM task_logs
                WHERE group_id = ? AND user_id = ? AND task_date = ?
                """,
                (group_id, user_id, today),
            ).fetchall()
            done_ids = {int(row["task_id"]) for row in done_rows}

            result: list[dict[str, Any]] = []
            for task in tasks:
                progress = self._task_progress_locked(
                    self._conn,
                    group_id,
                    user_id,
                    str(task["task_type"]),
                    today,
                )
                item = dict(task)
                item["progress"] = min(progress, float(task["target_value"]))
                item["completed"] = int(task["id"]) in done_ids
                result.append(item)
        return result

    def add_task(
        self,
        group_id: str,
        name: str,
        task_type: str,
        target_value: int,
        reward_points: int,
        reward_exp: int,
    ) -> dict[str, Any]:
        """管理员添加每日任务。

        ``task_type`` 支持中文（签到/发言/连续签到/积分）或英文类型。

        Returns:
            ``{"ok": True, ...}`` 或 ``{"ok": False, "reason": ...}``。
        """
        self.ensure_group_defaults(group_id)

        name = (name or "").strip()
        raw_type = (task_type or "").strip().lower()
        resolved_type = TASK_TYPE_ALIASES.get(task_type) or TASK_TYPE_ALIASES.get(
            raw_type,
        )
        if resolved_type is None:
            return {"ok": False, "reason": "unknown_type"}

        try:
            target_value = int(target_value)
            reward_points = int(reward_points)
            reward_exp = int(reward_exp)
        except (TypeError, ValueError):
            return {"ok": False, "reason": "invalid"}

        # 未指定目标次数时按任务类型填充默认值
        if target_value <= 0:
            target_value = TASK_DEFAULT_TARGET.get(resolved_type, 1)

        now = self._now_text()
        with self._tx() as conn:
            existing = conn.execute(
                """
                SELECT id FROM daily_tasks
                WHERE group_id = ? AND name = ?
                """,
                (group_id, name),
            ).fetchone()
            if existing is not None:
                return {
                    "ok": False,
                    "reason": "duplicate",
                    "task_id": int(existing["id"]),
                }

            cursor = conn.execute(
                """
                INSERT INTO daily_tasks
                    (group_id, code, name, task_type, target_value,
                     reward_points, reward_exp, is_default, created_at)
                VALUES (?, '', ?, ?, ?, ?, ?, 0, ?)
                """,
                (
                    group_id,
                    name,
                    resolved_type,
                    target_value,
                    reward_points,
                    reward_exp,
                    now,
                ),
            )
            task_id = int(cursor.lastrowid or 0)

        return {
            "ok": True,
            "task_id": task_id,
            "name": name,
            "task_type": resolved_type,
            "target_value": target_value,
            "reward_points": reward_points,
            "reward_exp": reward_exp,
        }

    def delete_task(self, group_id: str, task_id: int) -> bool:
        """删除指定任务，返回是否删除成功（历史完成记录保留）。"""
        with self._tx() as conn:
            cursor = conn.execute(
                """
                DELETE FROM daily_tasks
                WHERE group_id = ? AND id = ?
                """,
                (group_id, int(task_id)),
            )
            return cursor.rowcount > 0

    def get_task(self, group_id: str, task_id: int) -> dict[str, Any] | None:
        """获取单个任务。"""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT id, name, task_type, target_value,
                       reward_points, reward_exp, is_default
                FROM daily_tasks
                WHERE group_id = ? AND id = ?
                """,
                (group_id, int(task_id)),
            ).fetchone()
        return dict(row) if row else None

    def check_tasks(
        self,
        group_id: str,
        user_id: str,
        nickname: str = "",
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """检测并结算今天已完成的任务。

        每个任务每人每天只结算一次；奖励积分与经验在同一事务内发放。

        Returns:
            新完成的任务列表，每项包含任务名称、奖励与升级信息。
        """
        self.ensure_group_defaults(group_id)
        now = now or datetime.now()
        today = now.strftime("%Y-%m-%d")
        now_text = now.strftime("%Y-%m-%d %H:%M:%S")
        nickname = (nickname or "").strip()

        completed: list[dict[str, Any]] = []
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO users
                    (user_id, group_id, nickname, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(group_id, user_id) DO UPDATE SET
                    nickname = CASE
                        WHEN excluded.nickname <> '' THEN excluded.nickname
                        ELSE users.nickname
                    END,
                    updated_at = excluded.updated_at
                """,
                (user_id, group_id, nickname, now_text, now_text),
            )

            tasks = conn.execute(
                """
                SELECT id, name, task_type, target_value,
                       reward_points, reward_exp
                FROM daily_tasks
                WHERE group_id = ?
                ORDER BY id ASC
                """,
                (group_id,),
            ).fetchall()
            done_rows = conn.execute(
                """
                SELECT task_id FROM task_logs
                WHERE group_id = ? AND user_id = ? AND task_date = ?
                """,
                (group_id, user_id, today),
            ).fetchall()
            done_ids = {int(row["task_id"]) for row in done_rows}

            for task in tasks:
                task_id = int(task["id"])
                if task_id in done_ids:
                    continue

                progress = self._task_progress_locked(
                    conn,
                    group_id,
                    user_id,
                    str(task["task_type"]),
                    today,
                )
                if progress + 1e-9 < float(task["target_value"]):
                    continue

                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO task_logs
                        (task_id, group_id, user_id, task_date,
                         reward_points, reward_exp, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task_id,
                        group_id,
                        user_id,
                        today,
                        int(task["reward_points"]),
                        int(task["reward_exp"]),
                        now_text,
                    ),
                )
                if cursor.rowcount <= 0:
                    continue

                user_row = conn.execute(
                    """
                    SELECT points, exp, level, title FROM users
                    WHERE group_id = ? AND user_id = ?
                    """,
                    (group_id, user_id),
                ).fetchone()
                old_points = round(float(user_row["points"]), 2) if user_row else 0.0
                old_exp = int(user_row["exp"] or 0) if user_row else 0
                old_level = int(user_row["level"] or 1) if user_row else 1

                reward_points = int(task["reward_points"])
                reward_exp = int(task["reward_exp"])
                new_points = round(old_points + reward_points, 2)
                new_exp = old_exp + reward_exp
                new_level = level_for_exp(new_exp)
                new_title = title_for_level(new_level)

                conn.execute(
                    """
                    UPDATE users
                    SET points = ?, exp = ?, level = ?, title = ?, updated_at = ?
                    WHERE group_id = ? AND user_id = ?
                    """,
                    (
                        new_points,
                        new_exp,
                        new_level,
                        new_title,
                        now_text,
                        group_id,
                        user_id,
                    ),
                )
                self._insert_points_log(
                    conn,
                    group_id,
                    user_id,
                    reward_points,
                    new_points,
                    "任务",
                    f"完成任务「{task['name']}」",
                    now_text,
                )

                completed.append(
                    {
                        "task_id": task_id,
                        "name": str(task["name"]),
                        "reward_points": reward_points,
                        "reward_exp": reward_exp,
                        "points": new_points,
                        "exp": new_exp,
                        "level": new_level,
                        "title": new_title,
                        "old_level": old_level,
                        "level_up": new_level > old_level,
                    },
                )

        return completed

    # ==================================================================
    # v3.0.0 成就系统
    # ==================================================================
    @staticmethod
    def _achievement_value_locked(
        conn: sqlite3.Connection,
        group_id: str,
        user_id: str,
        condition_type: str,
        today: str,
    ) -> float:
        """计算某项成就条件当前的达成值（调用方需持有事务）。"""
        user_row = conn.execute(
            """
            SELECT points, exp, level, total_signs, streak
            FROM users
            WHERE group_id = ? AND user_id = ?
            """,
            (group_id, user_id),
        ).fetchone()

        if condition_type == "total_signs":
            return float(user_row["total_signs"] or 0) if user_row else 0.0
        if condition_type == "streak":
            return float(user_row["streak"] or 0) if user_row else 0.0
        if condition_type == "points":
            return float(user_row["points"] or 0) if user_row else 0.0
        if condition_type == "level":
            return float(user_row["level"] or 1) if user_row else 1.0
        if condition_type == "daily_chat":
            row = conn.execute(
                """
                SELECT message_count FROM daily_stats
                WHERE group_id = ? AND user_id = ? AND stat_date = ?
                """,
                (group_id, user_id, today),
            ).fetchone()
            return float(row["message_count"]) if row else 0.0
        if condition_type == "exchanges":
            row = conn.execute(
                """
                SELECT COUNT(*) AS cnt FROM exchange_logs
                WHERE group_id = ? AND user_id = ?
                """,
                (group_id, user_id),
            ).fetchone()
            return float(row["cnt"]) if row else 0.0
        if condition_type == "lottery_wins":
            row = conn.execute(
                """
                SELECT COUNT(*) AS cnt FROM lottery_logs
                WHERE group_id = ? AND user_id = ? AND is_win = 1
                """,
                (group_id, user_id),
            ).fetchone()
            return float(row["cnt"]) if row else 0.0
        if condition_type == "transfer_amount":
            row = conn.execute(
                """
                SELECT COALESCE(MAX(amount), 0) AS max_amount
                FROM transfer_logs
                WHERE group_id = ? AND from_user = ?
                """,
                (group_id, user_id),
            ).fetchone()
            return float(row["max_amount"]) if row else 0.0

        return 0.0

    def check_achievements(
        self,
        group_id: str,
        user_id: str,
        nickname: str = "",
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """检测并发放新达成的成就。

        成就按群记录，已达成过的成就不会重复发放奖励。

        Returns:
            新达成成就列表，每项包含成就名称、描述、奖励与升级信息。
        """
        self.ensure_group_defaults(group_id)
        now = now or datetime.now()
        today = now.strftime("%Y-%m-%d")
        now_text = now.strftime("%Y-%m-%d %H:%M:%S")
        nickname = (nickname or "").strip()

        unlocked: list[dict[str, Any]] = []
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO users
                    (user_id, group_id, nickname, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(group_id, user_id) DO UPDATE SET
                    nickname = CASE
                        WHEN excluded.nickname <> '' THEN excluded.nickname
                        ELSE users.nickname
                    END,
                    updated_at = excluded.updated_at
                """,
                (user_id, group_id, nickname, now_text, now_text),
            )

            rows = conn.execute(
                """
                SELECT a.id, a.code, a.name, a.description,
                       a.condition_type, a.target_value,
                       a.reward_points, a.reward_exp
                FROM achievements AS a
                LEFT JOIN user_achievements AS ua
                    ON ua.achievement_id = a.id
                   AND ua.group_id = ?
                   AND ua.user_id = ?
                WHERE ua.id IS NULL
                ORDER BY a.id ASC
                """,
                (group_id, user_id),
            ).fetchall()

            for achievement in rows:
                value = self._achievement_value_locked(
                    conn,
                    group_id,
                    user_id,
                    str(achievement["condition_type"]),
                    today,
                )
                # “单次转账超过 100 积分”使用严格大于，其余条件为达到目标值
                target_value = float(achievement["target_value"])
                if str(achievement["condition_type"]) == "transfer_amount":
                    reached = value > target_value + 1e-9
                else:
                    reached = value + 1e-9 >= target_value
                if not reached:
                    continue

                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO user_achievements
                        (achievement_id, group_id, user_id, achieved_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (int(achievement["id"]), group_id, user_id, now_text),
                )
                if cursor.rowcount <= 0:
                    continue

                user_row = conn.execute(
                    """
                    SELECT points, exp, level, title FROM users
                    WHERE group_id = ? AND user_id = ?
                    """,
                    (group_id, user_id),
                ).fetchone()
                old_points = round(float(user_row["points"]), 2) if user_row else 0.0
                old_exp = int(user_row["exp"] or 0) if user_row else 0
                old_level = int(user_row["level"] or 1) if user_row else 1

                reward_points = int(achievement["reward_points"])
                reward_exp = int(achievement["reward_exp"])
                new_points = round(old_points + reward_points, 2)
                new_exp = old_exp + reward_exp
                new_level = level_for_exp(new_exp)
                new_title = title_for_level(new_level)

                conn.execute(
                    """
                    UPDATE users
                    SET points = ?, exp = ?, level = ?, title = ?, updated_at = ?
                    WHERE group_id = ? AND user_id = ?
                    """,
                    (
                        new_points,
                        new_exp,
                        new_level,
                        new_title,
                        now_text,
                        group_id,
                        user_id,
                    ),
                )
                self._insert_points_log(
                    conn,
                    group_id,
                    user_id,
                    reward_points,
                    new_points,
                    "成就",
                    f"达成成就「{achievement['name']}」",
                    now_text,
                )

                unlocked.append(
                    {
                        "achievement_id": int(achievement["id"]),
                        "code": str(achievement["code"]),
                        "name": str(achievement["name"]),
                        "description": str(achievement["description"]),
                        "reward_points": reward_points,
                        "reward_exp": reward_exp,
                        "points": new_points,
                        "exp": new_exp,
                        "level": new_level,
                        "title": new_title,
                        "old_level": old_level,
                        "level_up": new_level > old_level,
                    },
                )

        return unlocked

    def list_achievements(
        self,
        group_id: str,
        user_id: str = "",
    ) -> list[dict[str, Any]]:
        """列出全部成就，并标记指定用户是否已达成。"""
        self.ensure_group_defaults(group_id)
        with self._lock:
            if user_id:
                rows = self._conn.execute(
                    """
                    SELECT a.id, a.code, a.name, a.description,
                           a.condition_type, a.target_value,
                           a.reward_points, a.reward_exp,
                           CASE WHEN ua.id IS NULL THEN 0 ELSE 1 END AS achieved,
                           ua.achieved_at
                    FROM achievements AS a
                    LEFT JOIN user_achievements AS ua
                        ON ua.achievement_id = a.id
                       AND ua.group_id = ?
                       AND ua.user_id = ?
                    ORDER BY a.id ASC
                    """,
                    (group_id, user_id),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    """
                    SELECT id, code, name, description, condition_type,
                           target_value, reward_points, reward_exp,
                           0 AS achieved, NULL AS achieved_at
                    FROM achievements
                    ORDER BY id ASC
                    """,
                ).fetchall()
        return [dict(row) for row in rows]

    def get_user_achievements(
        self,
        group_id: str,
        user_id: str,
    ) -> list[dict[str, Any]]:
        """获取指定用户已达成的成就（按达成时间倒序）。"""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT a.id, a.code, a.name, a.description,
                       a.condition_type, a.target_value,
                       a.reward_points, a.reward_exp, ua.achieved_at
                FROM user_achievements AS ua
                JOIN achievements AS a ON a.id = ua.achievement_id
                WHERE ua.group_id = ? AND ua.user_id = ?
                ORDER BY ua.id DESC
                """,
                (group_id, user_id),
            ).fetchall()
        return [dict(row) for row in rows]

    # ==================================================================
    # v3.0.0 积分转账
    # ==================================================================
    def transfer_points(
        self,
        group_id: str,
        from_user: str,
        to_user: str,
        amount: float,
        from_name: str = "",
        to_name: str = "",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """转账积分给其他成员。

        规则：
        - 手续费 5%，最低 1 积分（从转出方额外扣除）；
        - 每人每天最多转出 500 积分（按转账本金计算）；
        - 注册不满 7 天的新成员不能转账；
        - 余额不足时拒绝转账。

        Returns:
            ``status`` 可能为：``ok``、``same_user``、``invalid_amount``、
            ``too_young``、``daily_limit``、``insufficient``。
        """
        now = now or datetime.now()
        today = now.strftime("%Y-%m-%d")
        now_text = now.strftime("%Y-%m-%d %H:%M:%S")
        now_ts = now.timestamp()
        from_name = (from_name or "").strip()
        to_name = (to_name or "").strip()

        try:
            amount = round(float(amount), 2)
        except (TypeError, ValueError):
            return {"status": "invalid_amount"}
        if amount <= 0 or amount > TRANSFER_DAILY_LIMIT:
            return {"status": "invalid_amount"}
        if from_user == to_user:
            return {"status": "same_user"}

        fee = max(float(TRANSFER_MIN_FEE), round(amount * TRANSFER_FEE_RATE, 2))
        total = round(amount + fee, 2)

        with self._tx() as conn:
            # 确保双方用户记录存在
            for uid, name in ((from_user, from_name), (to_user, to_name)):
                conn.execute(
                    """
                    INSERT INTO users
                        (user_id, group_id, nickname, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(group_id, user_id) DO UPDATE SET
                        nickname = CASE
                            WHEN excluded.nickname <> '' THEN excluded.nickname
                            ELSE users.nickname
                        END,
                        updated_at = excluded.updated_at
                    """,
                    (uid, group_id, name, now_text, now_text),
                )

            from_row = conn.execute(
                """
                SELECT points, created_at FROM users
                WHERE group_id = ? AND user_id = ?
                """,
                (group_id, from_user),
            ).fetchone()
            from_points = round(float(from_row["points"]), 2) if from_row else 0.0
            created_at = str(from_row["created_at"] or "") if from_row else ""

            # 新成员注册满 7 天后才能转账
            if created_at:
                try:
                    created = datetime.strptime(
                        created_at,
                        "%Y-%m-%d %H:%M:%S",
                    )
                    age_seconds = (now - created).total_seconds()
                    if age_seconds < TRANSFER_MIN_AGE_DAYS * 86400:
                        days_left = max(
                            1,
                            int(
                                math.ceil(
                                    (TRANSFER_MIN_AGE_DAYS * 86400 - age_seconds)
                                    / 86400,
                                ),
                            ),
                        )
                        return {"status": "too_young", "days_left": days_left}
                except (TypeError, ValueError):
                    # 时间格式异常时不做限制，避免阻塞正常使用
                    pass

            # 今日已转出本金
            used_row = conn.execute(
                """
                SELECT COALESCE(SUM(amount), 0) AS used
                FROM transfer_logs
                WHERE group_id = ? AND from_user = ?
                  AND substr(created_at, 1, 10) = ?
                """,
                (group_id, from_user, today),
            ).fetchone()
            daily_used = round(float(used_row["used"]), 2) if used_row else 0.0
            if daily_used + amount > TRANSFER_DAILY_LIMIT + 1e-9:
                return {
                    "status": "daily_limit",
                    "daily_used": daily_used,
                    "daily_remaining": round(
                        max(0.0, TRANSFER_DAILY_LIMIT - daily_used),
                        2,
                    ),
                }

            if from_points + 1e-9 < total:
                return {
                    "status": "insufficient",
                    "points": from_points,
                    "need": total,
                    "fee": fee,
                }

            to_row = conn.execute(
                """
                SELECT points FROM users
                WHERE group_id = ? AND user_id = ?
                """,
                (group_id, to_user),
            ).fetchone()
            to_points = round(float(to_row["points"]), 2) if to_row else 0.0

            new_from_points = round(from_points - total, 2)
            new_to_points = round(to_points + amount, 2)

            conn.execute(
                """
                UPDATE users SET points = ?, updated_at = ?
                WHERE group_id = ? AND user_id = ?
                """,
                (new_from_points, now_text, group_id, from_user),
            )
            conn.execute(
                """
                UPDATE users SET points = ?, updated_at = ?
                WHERE group_id = ? AND user_id = ?
                """,
                (new_to_points, now_text, group_id, to_user),
            )
            conn.execute(
                """
                INSERT INTO transfer_logs
                    (group_id, from_user, to_user, amount, fee, total,
                     created_at, created_ts)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    group_id,
                    from_user,
                    to_user,
                    amount,
                    fee,
                    total,
                    now_text,
                    now_ts,
                ),
            )

            self._insert_points_log(
                conn,
                group_id,
                from_user,
                -total,
                new_from_points,
                "转账",
                f"转账给 {to_name or to_user}（含手续费 {fee:.2f}）",
                now_text,
            )
            self._insert_points_log(
                conn,
                group_id,
                to_user,
                amount,
                new_to_points,
                "转账",
                f"收到 {from_name or from_user} 的转账",
                now_text,
            )

        return {
            "status": "ok",
            "amount": amount,
            "fee": fee,
            "total": total,
            "points": new_from_points,
            "to_points": new_to_points,
            "daily_used": round(daily_used + amount, 2),
            "daily_remaining": round(
                max(0.0, TRANSFER_DAILY_LIMIT - daily_used - amount),
                2,
            ),
        }

    def get_transfer_logs(
        self,
        group_id: str,
        user_id: str,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """获取与指定用户相关的转账记录（转出或转入）。"""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, from_user, to_user, amount, fee, total, created_at
                FROM transfer_logs
                WHERE group_id = ? AND (from_user = ? OR to_user = ?)
                ORDER BY id DESC
                LIMIT ?
                """,
                (group_id, user_id, user_id, int(limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    # ==================================================================
    # v3.0.0 积分明细
    # ==================================================================
    def get_points_logs(
        self,
        group_id: str,
        user_id: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """获取个人最近若干条积分变动明细（按时间倒序）。"""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, change, balance, log_type, detail, created_at
                FROM points_logs
                WHERE group_id = ? AND user_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (group_id, user_id, int(limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    # ==================================================================
    # v4.0.0 群管系统：违禁词
    # ==================================================================
    def add_banned_word(
        self,
        group_id: str,
        word: str,
        added_by: str = "",
        punish_type: str = DEFAULT_BANNED_PUNISH,
        punish_duration: int = DEFAULT_BANNED_DURATION,
    ) -> dict[str, Any]:
        """添加违禁词。

        Returns:
            ``{"ok": bool, "reason": 失败原因, "word": 词, ...}``
        """
        word = str(word or "").strip()
        if not word:
            return {"ok": False, "reason": "违禁词不能为空"}
        if len(word) > 50:
            return {"ok": False, "reason": "违禁词长度不能超过 50 个字符"}

        punish = BANNED_PUNISH_ALIASES.get(str(punish_type or "").strip().lower())
        if punish is None:
            return {
                "ok": False,
                "reason": "惩罚类型只能是：警告 / 禁言 / 踢出 / 积分扣除",
            }

        try:
            duration = int(punish_duration)
        except (TypeError, ValueError):
            duration = DEFAULT_BANNED_DURATION

        # 禁言时长与扣分数量使用同一字段存储，分别限制取值范围
        if punish == "禁言":
            duration = max(1, min(MAX_MUTE_MINUTES, duration or DEFAULT_BANNED_DURATION))
        elif punish == "积分扣除":
            duration = max(1, min(100000, duration or DEFAULT_BANNED_DURATION))
        else:
            duration = 0

        now = self._now_text()
        with self._tx() as conn:
            exists = conn.execute(
                """
                SELECT id FROM banned_words
                WHERE group_id = ? AND LOWER(word) = LOWER(?)
                """,
                (group_id, word),
            ).fetchone()
            if exists is not None:
                return {
                    "ok": False,
                    "reason": f"违禁词「{word}」已经存在",
                }

            cursor = conn.execute(
                """
                INSERT INTO banned_words
                    (group_id, word, added_by, punish_type,
                     punish_duration, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (group_id, word, added_by or "", punish, duration, now),
            )
            word_id = int(cursor.lastrowid or 0)

        return {
            "ok": True,
            "id": word_id,
            "word": word,
            "punish_type": punish,
            "punish_duration": duration,
        }

    def delete_banned_word(self, group_id: str, word: str) -> bool:
        """删除违禁词，返回是否删除成功。"""
        word = str(word or "").strip()
        if not word:
            return False
        with self._tx() as conn:
            cursor = conn.execute(
                """
                DELETE FROM banned_words
                WHERE group_id = ? AND LOWER(word) = LOWER(?)
                """,
                (group_id, word),
            )
            return int(cursor.rowcount or 0) > 0

    def list_banned_words(self, group_id: str) -> list[dict[str, Any]]:
        """列出本群全部违禁词（按添加顺序）。"""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, group_id, word, added_by, punish_type,
                       punish_duration, created_at
                FROM banned_words
                WHERE group_id = ?
                ORDER BY id ASC
                """,
                (group_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    # ==================================================================
    # v4.0.0 群管系统：惩罚与警告记录
    # ==================================================================
    def add_punish_log(
        self,
        group_id: str,
        user_id: str,
        punish_type: str,
        reason: str = "",
        duration: int = 0,
        executor_id: str = "",
    ) -> int:
        """写入一条惩罚记录，返回记录 ID。"""
        now = datetime.now()
        with self._tx() as conn:
            cursor = conn.execute(
                """
                INSERT INTO punish_logs
                    (group_id, user_id, punish_type, reason, duration,
                     executor_id, created_at, created_ts)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    group_id,
                    user_id,
                    str(punish_type or "").strip() or "未知",
                    str(reason or "").strip()[:200],
                    int(duration or 0),
                    str(executor_id or ""),
                    now.strftime("%Y-%m-%d %H:%M:%S"),
                    now.timestamp(),
                ),
            )
            return int(cursor.lastrowid or 0)

    def get_punish_logs(
        self,
        group_id: str,
        user_id: str,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """查看指定成员的违规/惩罚历史。"""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, punish_type, reason, duration,
                       executor_id, created_at
                FROM punish_logs
                WHERE group_id = ? AND user_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (group_id, user_id, int(limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    def add_warning(
        self,
        group_id: str,
        user_id: str,
        reason: str,
        warned_by: str = "",
    ) -> dict[str, Any]:
        """记录一次警告，同时写入惩罚记录表。"""
        now = datetime.now()
        reason = str(reason or "").strip()[:200] or "未填写原因"
        with self._tx() as conn:
            cursor = conn.execute(
                """
                INSERT INTO warnings
                    (group_id, user_id, reason, warned_by, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    group_id,
                    user_id,
                    reason,
                    str(warned_by or ""),
                    now.strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
            warning_id = int(cursor.lastrowid or 0)
            conn.execute(
                """
                INSERT INTO punish_logs
                    (group_id, user_id, punish_type, reason, duration,
                     executor_id, created_at, created_ts)
                VALUES (?, ?, '警告', ?, 0, ?, ?, ?)
                """,
                (
                    group_id,
                    user_id,
                    reason,
                    str(warned_by or ""),
                    now.strftime("%Y-%m-%d %H:%M:%S"),
                    now.timestamp(),
                ),
            )
        return {
            "id": warning_id,
            "reason": reason,
            "created_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        }

    def get_warnings(
        self,
        group_id: str,
        user_id: str,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """查看指定成员的警告记录。"""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, reason, warned_by, created_at
                FROM warnings
                WHERE group_id = ? AND user_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (group_id, user_id, int(limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    def clear_violations(self, group_id: str, user_id: str) -> dict[str, int]:
        """清空指定成员的警告与惩罚记录，返回删除条数。"""
        with self._tx() as conn:
            warning_count = int(
                conn.execute(
                    "DELETE FROM warnings WHERE group_id = ? AND user_id = ?",
                    (group_id, user_id),
                ).rowcount
                or 0,
            )
            punish_count = int(
                conn.execute(
                    "DELETE FROM punish_logs WHERE group_id = ? AND user_id = ?",
                    (group_id, user_id),
                ).rowcount
                or 0,
            )
        return {"warnings": warning_count, "punishments": punish_count}

    # ==================================================================
    # v4.0.0 群管系统：自动审核配置
    # ==================================================================
    @staticmethod
    def default_auto_mod_settings() -> dict[str, dict[str, Any]]:
        """返回自动审核的默认配置（每个群首次使用时）。"""
        return {
            "anti_spam": {
                "enabled": False,
                "threshold": DEFAULT_SPAM_THRESHOLD,
                "window": DEFAULT_SPAM_WINDOW,
                "mute_minutes": DEFAULT_SPAM_MUTE_MINUTES,
            },
            "anti_ad": {
                "enabled": False,
                "mute_minutes": DEFAULT_AD_MUTE_MINUTES,
            },
            "night_mode": {
                "enabled": False,
                "mute_minutes": DEFAULT_NIGHT_MUTE_MINUTES,
            },
        }

    def get_auto_mod_settings(self, group_id: str) -> dict[str, dict[str, Any]]:
        """读取本群自动审核配置，缺失项使用默认值填充。"""
        settings = self.default_auto_mod_settings()
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT feature, enabled, config
                FROM auto_mod_config
                WHERE group_id = ?
                """,
                (group_id,),
            ).fetchall()

        for row in rows:
            feature = str(row["feature"])
            if feature not in settings:
                continue
            try:
                config = json.loads(row["config"] or "{}")
            except (TypeError, ValueError):
                config = {}
            if isinstance(config, dict):
                settings[feature].update(config)
            settings[feature]["enabled"] = bool(row["enabled"])
        return settings

    def set_auto_mod_feature(
        self,
        group_id: str,
        feature: str,
        enabled: bool | None = None,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """更新自动审核开关或配置参数，返回更新后的该功能配置。"""
        if feature not in AUTO_MOD_FEATURES:
            raise ValueError(f"未知的自动审核功能：{feature}")

        settings = self.get_auto_mod_settings(group_id)
        current = dict(settings[feature])
        new_enabled = (
            bool(enabled) if enabled is not None else bool(settings[feature]["enabled"])
        )
        if config:
            current.update(config)
        current.pop("enabled", None)

        now = self._now_text()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO auto_mod_config
                    (group_id, feature, enabled, config, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(group_id, feature) DO UPDATE SET
                    enabled = excluded.enabled,
                    config = excluded.config,
                    updated_at = excluded.updated_at
                """,
                (
                    group_id,
                    feature,
                    int(new_enabled),
                    json.dumps(current, ensure_ascii=False),
                    now,
                ),
            )
            self._conn.commit()
        return self.get_auto_mod_settings(group_id)[feature]

    # ==================================================================
    # v4.0.0 群管系统：白名单
    # ==================================================================
    def add_whitelist(
        self,
        group_id: str,
        user_id: str,
        added_by: str = "",
    ) -> bool:
        """添加白名单成员，已存在时返回 False。"""
        now = self._now_text()
        with self._tx() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO whitelist
                    (group_id, user_id, added_by, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (group_id, user_id, added_by or "", now),
            )
            return int(cursor.rowcount or 0) > 0

    def remove_whitelist(self, group_id: str, user_id: str) -> bool:
        """移除白名单成员，返回是否删除成功。"""
        with self._tx() as conn:
            cursor = conn.execute(
                "DELETE FROM whitelist WHERE group_id = ? AND user_id = ?",
                (group_id, user_id),
            )
            return int(cursor.rowcount or 0) > 0

    def list_whitelist(self, group_id: str) -> list[dict[str, Any]]:
        """列出本群白名单成员。"""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT w.user_id, w.added_by, w.created_at,
                       u.nickname
                FROM whitelist AS w
                LEFT JOIN users AS u
                    ON u.group_id = w.group_id AND u.user_id = w.user_id
                WHERE w.group_id = ?
                ORDER BY w.created_at ASC, w.user_id ASC
                """,
                (group_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def is_whitelisted(self, group_id: str, user_id: str) -> bool:
        """判断成员是否在白名单中。"""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT 1 FROM whitelist
                WHERE group_id = ? AND user_id = ?
                """,
                (group_id, user_id),
            ).fetchone()
        return row is not None

    # ==================================================================
    # v4.0.0 群管系统：公告
    # ==================================================================
    def add_announcement(
        self,
        group_id: str,
        content: str,
        publisher_id: str = "",
        scheduled_at: str | None = None,
    ) -> dict[str, Any]:
        """新增公告。

        ``scheduled_at`` 为空表示立即发布；否则为 ``YYYY-MM-DD HH:MM:SS``，
        由后台任务在到点后发送。
        """
        content = str(content or "").strip()
        now = self._now_text()
        is_scheduled = 1 if scheduled_at else 0
        sent = 0 if is_scheduled else 1

        with self._tx() as conn:
            cursor = conn.execute(
                """
                INSERT INTO announcements
                    (group_id, content, publisher_id, created_at,
                     is_scheduled, scheduled_at, sent)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    group_id,
                    content,
                    publisher_id or "",
                    now,
                    is_scheduled,
                    scheduled_at or None,
                    sent,
                ),
            )
            announcement_id = int(cursor.lastrowid or 0)

        return {
            "id": announcement_id,
            "content": content,
            "is_scheduled": bool(is_scheduled),
            "scheduled_at": scheduled_at or "",
        }

    def list_announcements(
        self,
        group_id: str,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """列出本群历史公告（包含未到点的定时公告）。"""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, content, publisher_id, created_at,
                       is_scheduled, scheduled_at, sent
                FROM announcements
                WHERE group_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (group_id, int(limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_announcement(self, group_id: str, announcement_id: int) -> bool:
        """删除公告，返回是否删除成功。"""
        with self._tx() as conn:
            cursor = conn.execute(
                """
                DELETE FROM announcements
                WHERE group_id = ? AND id = ?
                """,
                (group_id, int(announcement_id)),
            )
            return int(cursor.rowcount or 0) > 0

    def get_due_announcements(
        self,
        now_text: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """获取所有已到发送时间且未发送的定时公告。"""
        now_text = now_text or self._now_text()
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, group_id, content, publisher_id, scheduled_at
                FROM announcements
                WHERE is_scheduled = 1
                  AND sent = 0
                  AND scheduled_at IS NOT NULL
                  AND scheduled_at <= ?
                ORDER BY scheduled_at ASC, id ASC
                LIMIT ?
                """,
                (now_text, int(limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_announcement_sent(self, announcement_id: int) -> None:
        """把定时公告标记为已发送。"""
        with self._lock:
            self._conn.execute(
                "UPDATE announcements SET sent = 1 WHERE id = ?",
                (int(announcement_id),),
            )
            self._conn.commit()

    def get_group_origin(self, group_id: str) -> str:
        """获取群的 unified_msg_origin，用于后台主动发送消息。"""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT unified_msg_origin FROM group_settings
                WHERE group_id = ?
                """,
                (group_id,),
            ).fetchone()
        return str(row["unified_msg_origin"] or "") if row else ""

    # ==================================================================
    # v4.0.0 群管系统：群数据统计
    # ==================================================================
    def get_group_stats(
        self,
        group_id: str,
        today: str | None = None,
    ) -> dict[str, Any]:
        """汇总班级群整体数据。"""
        today = today or datetime.now().strftime("%Y-%m-%d")
        with self._lock:
            total_users = int(
                self._conn.execute(
                    "SELECT COUNT(*) FROM users WHERE group_id = ?",
                    (group_id,),
                ).fetchone()[0]
                or 0,
            )
            active_row = self._conn.execute(
                """
                SELECT COUNT(*) AS active, COALESCE(SUM(message_count), 0) AS total
                FROM daily_stats
                WHERE group_id = ? AND stat_date = ?
                """,
                (group_id, today),
            ).fetchone()
            points_row = self._conn.execute(
                """
                SELECT COALESCE(SUM(points), 0) AS total,
                       COALESCE(AVG(points), 0) AS average
                FROM users
                WHERE group_id = ?
                """,
                (group_id,),
            ).fetchone()
            punish_rows = self._conn.execute(
                """
                SELECT punish_type, COUNT(*) AS count
                FROM punish_logs
                WHERE group_id = ?
                GROUP BY punish_type
                """,
                (group_id,),
            ).fetchall()
            warning_count = int(
                self._conn.execute(
                    "SELECT COUNT(*) FROM warnings WHERE group_id = ?",
                    (group_id,),
                ).fetchone()[0]
                or 0,
            )
            whitelist_count = int(
                self._conn.execute(
                    "SELECT COUNT(*) FROM whitelist WHERE group_id = ?",
                    (group_id,),
                ).fetchone()[0]
                or 0,
            )
            banned_count = int(
                self._conn.execute(
                    "SELECT COUNT(*) FROM banned_words WHERE group_id = ?",
                    (group_id,),
                ).fetchone()[0]
                or 0,
            )

        punish_counts = {
            str(row["punish_type"]): int(row["count"]) for row in punish_rows
        }
        return {
            "total_users": total_users,
            "active_today": int(active_row["active"] or 0),
            "today_messages": int(active_row["total"] or 0),
            "points_total": round(float(points_row["total"] or 0), 2),
            "points_average": round(float(points_row["average"] or 0), 2),
            "violation_total": sum(punish_counts.values()),
            "warn_count": punish_counts.get("警告", 0),
            "mute_count": punish_counts.get("禁言", 0),
            "kick_count": punish_counts.get("踢出", 0),
            "deduct_count": punish_counts.get("积分扣除", 0),
            "warning_records": warning_count,
            "whitelist_count": whitelist_count,
            "banned_word_count": banned_count,
        }

    def get_active_leaderboard(
        self,
        group_id: str,
        today: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """获取今日发言活跃度排行榜。"""
        today = today or datetime.now().strftime("%Y-%m-%d")
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT d.user_id,
                       COALESCE(u.nickname, '') AS nickname,
                       d.message_count,
                       COALESCE(u.points, 0) AS points
                FROM daily_stats AS d
                LEFT JOIN users AS u
                    ON u.group_id = d.group_id AND u.user_id = d.user_id
                WHERE d.group_id = ? AND d.stat_date = ?
                ORDER BY d.message_count DESC, d.user_id ASC
                LIMIT ?
                """,
                (group_id, today, int(limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_violation_leaderboard(
        self,
        group_id: str,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """获取违规次数排行榜。"""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT p.user_id,
                       COALESCE(u.nickname, '') AS nickname,
                       COUNT(*) AS violation_count,
                       SUM(CASE WHEN p.punish_type = '警告' THEN 1 ELSE 0 END)
                           AS warn_count,
                       SUM(CASE WHEN p.punish_type = '禁言' THEN 1 ELSE 0 END)
                           AS mute_count,
                       SUM(CASE WHEN p.punish_type = '踢出' THEN 1 ELSE 0 END)
                           AS kick_count
                FROM punish_logs AS p
                LEFT JOIN users AS u
                    ON u.group_id = p.group_id AND u.user_id = p.user_id
                WHERE p.group_id = ?
                GROUP BY p.user_id
                ORDER BY violation_count DESC, p.user_id ASC
                LIMIT ?
                """,
                (group_id, int(limit)),
            ).fetchall()
        return [dict(row) for row in rows]

