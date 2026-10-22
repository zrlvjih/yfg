#!/usr/bin/env python3
"""班级管理插件 v1.5.2 → v1.5.3 数据库迁移脚本。

用法（建议先停止 AstrBot 再执行）：

    python migrate.py
    python migrate.py /path/to/class_manager.db
    python migrate.py --no-backup

v1.5.3 是平台适配与禁言修复版，数据库结构无变化：
- 禁言能力由 ``platform_adapter.py`` 在运行时探测（OneBot / Telegram 支持，
  QQ 官方机器人不支持），不依赖数据库字段；
- 惩罚记录继续复用 v1.4.0 起就有的 ``punish_logs`` 表，
  新增的“积分扣除 / 解除禁言 / 手动惩罚”等类型直接写入 ``punish_type``；
- 本脚本仍会检查并补齐全部历史字段/数据表，方便从更早版本直接升级；
- 签到、积分、商城、抽奖、任务、成就、转账与群管数据全部原样保留。

脚本会：
1. 自动识别数据库版本（v1.0.0 ~ v1.5.3）；
2. 创建缺失的数据表（商城、抽奖、任务、成就、群管等）；
3. 补齐 users 表的 exp / level / title 字段；
4. 补齐 group_settings 表的 defaults_seeded / trigger_mode 字段；
5. 写入默认成就，并为已有群播种默认每日任务；
6. 为已有群写入默认自动审核配置（默认全部关闭，不改变原有行为）；
7. 按经验值重算等级与称号；
8. 迁移前默认生成数据库备份，迁移后执行完整性检查。

所有建表语句均为 ``CREATE TABLE IF NOT EXISTS``，可安全重复执行；
插件启动时 ``ClassDatabase`` 也会自动完成相同的建表与补列操作，
本脚本用于显式迁移、提前备份或在命令行确认迁移结果。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# 允许直接运行：python migrate.py
PLUGIN_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PLUGIN_DIR))

from database import (
    DEFAULT_ACHIEVEMENTS,
    DEFAULT_TASKS,
    NEW_GROUP_COLUMNS,
    NEW_USER_COLUMNS,
    SCHEMA,
    level_for_exp,
    title_for_level,
)

DEFAULT_DB_PATH = PLUGIN_DIR / "data" / "class_manager.db"

# 商城与抽奖等 v1.2.0 起引入的数据表
SHOP_TABLES = [
    "shop_items",
    "exchange_logs",
    "lottery_prizes",
    "lottery_logs",
]

# v1.3.0 起引入的任务、成就、转账与积分明细表
GROWTH_TABLES = [
    "daily_tasks",
    "task_logs",
    "achievements",
    "user_achievements",
    "transfer_logs",
    "points_logs",
    "daily_stats",
]

# v1.4.0 起引入的群管数据表
GROUP_TABLES = [
    "banned_words",
    "punish_logs",
    "auto_mod_config",
    "warnings",
    "announcements",
    "whitelist",
]

# v1.5.0 自动审核默认配置（与新群首次使用时的默认值一致）
DEFAULT_AUTO_MOD_CONFIG = {
    "anti_spam": {
        "enabled": 0,
        "config": {"threshold": 5, "window": 10, "mute_minutes": 30},
    },
    "anti_ad": {"enabled": 0, "config": {"mute_minutes": 60}},
    "night_mode": {"enabled": 0, "config": {"mute_minutes": 10}},
}


def backup_database(db_path: Path) -> Path:
    """使用 SQLite 在线备份 API 生成一致性备份。"""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = db_path.with_name(f"{db_path.name}.bak-{stamp}")

    source = sqlite3.connect(str(db_path))
    target = sqlite3.connect(str(backup_path))
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    return backup_path


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    """判断表是否存在。"""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def table_columns(conn: sqlite3.Connection, name: str) -> set[str]:
    """读取表的全部字段名。"""
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({name})")}


def detect_version(conn: sqlite3.Connection) -> str:
    """识别数据库版本，仅用于提示信息。"""
    if not table_exists(conn, "users"):
        return "未知"

    columns = table_columns(conn, "users")
    group_columns = (
        table_columns(conn, "group_settings")
        if table_exists(conn, "group_settings")
        else set()
    )

    if "trigger_mode" in group_columns:
        return "v1.5.3（结构已就绪）"

    has_group = all(table_exists(conn, name) for name in GROUP_TABLES)
    has_growth = all(table_exists(conn, name) for name in GROWTH_TABLES)
    has_shop = all(table_exists(conn, name) for name in SHOP_TABLES)
    has_v2 = {"exp", "level", "title"}.issubset(columns)

    if has_group:
        return "v1.4.0"
    if has_growth:
        return "v1.3.x"
    if has_shop and has_v2:
        return "v1.2.x"
    if has_v2:
        return "v1.1.x"
    return "v1.0.0"


def add_missing_columns(
    conn: sqlite3.Connection,
    table: str,
    columns: dict[str, str],
) -> list[str]:
    """检测并添加指定表缺失的字段，返回新增字段列表。"""
    existing = table_columns(conn, table)
    added: list[str] = []
    for column, ddl in columns.items():
        if column not in existing:
            conn.execute(ddl)
            added.append(column)
    return added


def recalculate_levels(conn: sqlite3.Connection) -> tuple[int, int]:
    """根据经验值重算等级与称号，返回 (用户总数, 实际更新数)。"""
    rows = conn.execute(
        "SELECT group_id, user_id, exp, level, title FROM users",
    ).fetchall()

    updates: list[tuple[int, str, str, str]] = []
    for row in rows:
        exp = int(row[2] or 0)
        level = level_for_exp(exp)
        title = title_for_level(level)
        if int(row[3] or 1) != level or str(row[4] or "") != title:
            updates.append((level, title, str(row[0]), str(row[1])))

    if updates:
        conn.executemany(
            """
            UPDATE users SET level = ?, title = ?
            WHERE group_id = ? AND user_id = ?
            """,
            updates,
        )
    return len(rows), len(updates)


def seed_achievements(conn: sqlite3.Connection) -> int:
    """写入默认成就定义（全局共享，按 code 去重），返回新增数量。"""
    inserted = 0
    for item in DEFAULT_ACHIEVEMENTS:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO achievements
                (code, name, description, condition_type,
                 target_value, reward_points, reward_exp)
            VALUES (?, ?, ?, ?, ?, ?, ?)
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
        inserted += max(0, cursor.rowcount)
    return inserted


def list_group_ids(conn: sqlite3.Connection) -> list[str]:
    """收集数据库中所有出现过的群 ID（用户表与群设置表取并集）。"""
    groups: set[str] = set()
    for table in ("users", "group_settings"):
        if not table_exists(conn, table):
            continue
        for row in conn.execute(f"SELECT DISTINCT group_id FROM {table}"):
            group_id = str(row[0] or "").strip()
            if group_id:
                groups.add(group_id)
    return sorted(groups)


def seed_default_tasks(conn: sqlite3.Connection) -> tuple[int, int]:
    """为每个群播种默认每日任务，返回 (群总数, 实际播种群数)。"""
    groups = list_group_ids(conn)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    seeded = 0

    for group_id in groups:
        row = conn.execute(
            "SELECT defaults_seeded FROM group_settings WHERE group_id = ?",
            (group_id,),
        ).fetchone()
        if row is not None and int(row[0] or 0) == 1:
            continue

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
        seeded += 1

    return len(groups), seeded


def seed_auto_mod_config(conn: sqlite3.Connection) -> tuple[int, int]:
    """为每个已有群写入默认自动审核配置，返回 (群总数, 写入记录数)。"""
    groups = list_group_ids(conn)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    inserted = 0

    for group_id in groups:
        for feature, item in DEFAULT_AUTO_MOD_CONFIG.items():
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO auto_mod_config
                    (group_id, feature, enabled, config, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    group_id,
                    feature,
                    int(item["enabled"]),
                    json.dumps(item["config"], ensure_ascii=False),
                    now,
                ),
            )
            inserted += max(0, cursor.rowcount)

    return len(groups), inserted


def migrate(db_path: Path, make_backup: bool = True) -> int:
    """执行迁移，返回进程退出码。"""
    print("=" * 64)
    print("班级管理插件 v1.5.2 → v1.5.3 数据库迁移（平台适配 / 禁言修复版）")
    print(f"数据库路径：{db_path}")
    print("=" * 64)

    if not db_path.exists():
        print("[提示] 数据库不存在，将在插件首次启动时自动创建 v1.5.3 结构。")
        return 0

    if make_backup:
        backup_path = backup_database(db_path)
        print(f"[备份] 已生成备份：{backup_path}")
    else:
        print("[备份] 已跳过备份（--no-backup）")

    conn = sqlite3.connect(str(db_path), isolation_level=None)
    try:
        if not table_exists(conn, "users"):
            print("[错误] 未找到 users 表，这可能不是班级管理插件的数据库。")
            return 1

        version = detect_version(conn)
        print(f"\n[检测] 当前数据库版本：{version}")

        print("\n[1/7] 创建商城、成长与群管数据表……")
        # SCHEMA 包含全部表定义，且均为 CREATE TABLE IF NOT EXISTS
        conn.executescript(SCHEMA)

        all_tables = SHOP_TABLES + GROWTH_TABLES + GROUP_TABLES
        missing = [name for name in all_tables if not table_exists(conn, name)]
        if missing:
            print(f"[错误] 以下数据表创建失败：{'、'.join(missing)}")
            return 1
        print(f"      商城/成长表已就绪：{'、'.join(SHOP_TABLES + GROWTH_TABLES)}")
        print(f"      群管表已就绪：{'、'.join(GROUP_TABLES)}")

        print("\n[2/7] 检测并补齐 users 表字段……")
        added_users = add_missing_columns(conn, "users", NEW_USER_COLUMNS)
        if added_users:
            print(f"      已添加字段：{'、'.join(added_users)}")
        else:
            print("      字段已完整（exp / level / title 均存在）")

        print("\n[3/7] 检测并补齐 group_settings 表字段……")
        added_groups = add_missing_columns(
            conn,
            "group_settings",
            NEW_GROUP_COLUMNS,
        )
        if added_groups:
            print(f"      已添加字段：{'、'.join(added_groups)}")
        else:
            print("      字段已完整（defaults_seeded / trigger_mode 均存在）")

        print("\n[4/7] 初始化默认成就定义……")
        achievements_added = seed_achievements(conn)
        print(
            f"      默认成就 {len(DEFAULT_ACHIEVEMENTS)} 项，"
            f"本次新增：{achievements_added}",
        )

        print("\n[5/7] 为已有群播种默认每日任务……")
        total_groups, seeded_groups = seed_default_tasks(conn)
        print(f"      群总数：{total_groups}，本次播种：{seeded_groups}")

        print("\n[6/7] 为已有群写入默认自动审核配置（默认全部关闭）……")
        mod_groups, mod_records = seed_auto_mod_config(conn)
        print(f"      群总数：{mod_groups}，写入配置记录：{mod_records}")

        print("\n[7/7] 重算等级并检查数据库完整性……")
        total_users, updated = recalculate_levels(conn)
        print(f"      用户总数：{total_users}，更新等级/称号：{updated}")

        result = conn.execute("PRAGMA integrity_check").fetchone()
        integrity = str(result[0]) if result else "unknown"
        print(f"      完整性检查：{integrity}")
        if integrity.lower() != "ok":
            print("[警告] 完整性检查未通过，请使用备份恢复后重试。")
            return 1

        print("\n[完成] 数据库已升级到 v1.5.3。")
        print("       原有签到、积分、商城、抽奖、任务、成就、转账与群管数据均已保留；")
        print("       惩罚记录继续使用 punish_logs 表，自动兼容积分扣除/解除禁言等新类型；")
        print("       禁言能力在运行时按平台自动探测，无数据库字段变更。")
        return 0
    except Exception as exc:
        print(f"\n[错误] 迁移失败：{exc}")
        print("       可使用迁移前生成的 .bak-* 备份恢复数据库。")
        return 1
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="班级管理插件 v1.5.2 → v1.5.3 数据库迁移",
    )
    parser.add_argument(
        "db_path",
        nargs="?",
        default=str(DEFAULT_DB_PATH),
        help=f"数据库路径，默认 {DEFAULT_DB_PATH}",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="迁移前不生成备份（不推荐）",
    )
    args = parser.parse_args()

    return migrate(Path(args.db_path).expanduser().resolve(), not args.no_backup)


if __name__ == "__main__":
    raise SystemExit(main())
