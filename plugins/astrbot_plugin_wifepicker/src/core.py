import asyncio
import heapq
import time
import os
from datetime import datetime, timedelta
from typing import Set

from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from ..onebot_api import extract_message_id
from .utils import (
    save_json,
    normalize_user_id_set,
    is_allowed_group,
    resolve_member_name,
)


ACTIVE_USERS_TRIM_INTERVAL_SECONDS = 3600


def count_active_users(active_users: dict) -> int:
    total = 0
    for users in active_users.values():
        if isinstance(users, dict):
            total += len(users)
    return total


def _get_active_users_trim_limit(plugin) -> int:
    raw = plugin.config.get("max_records", 500)
    try:
        max_total = int(raw)
    except Exception:
        max_total = 500
    return max(0, max_total)


def _recount_active_users(plugin) -> int:
    total = count_active_users(getattr(plugin, "active_users", {}))
    plugin._active_user_count = total
    return total


def _get_active_users_trim_overflow(plugin, max_total: int) -> int:
    if max_total <= 0:
        return 0
    return max(1, max_total // 20)


def _trim_active_users_to_limit(plugin, *, max_total: int | None = None) -> bool:
    if max_total is None:
        max_total = _get_active_users_trim_limit(plugin)

    current_total = getattr(plugin, "_active_user_count", None)
    if not isinstance(current_total, int) or current_total < 0:
        current_total = _recount_active_users(plugin)

    if current_total <= max_total:
        return False

    keep_heap: list[tuple[float, str, str]] = []
    if max_total > 0:
        for gid, users in plugin.active_users.items():
            if not isinstance(users, dict):
                continue
            for uid, ts in users.items():
                try:
                    ts_value = float(ts)
                except Exception:
                    ts_value = 0.0
                entry = (ts_value, str(gid), str(uid))
                if len(keep_heap) < max_total:
                    heapq.heappush(keep_heap, entry)
                elif ts_value > keep_heap[0][0]:
                    heapq.heapreplace(keep_heap, entry)

    new_active = {}
    for _, gid, uid in keep_heap:
        new_active.setdefault(gid, {})[uid] = plugin.active_users[gid][uid]

    plugin.active_users.clear()
    plugin.active_users.update(new_active)
    plugin._active_user_count = len(keep_heap)
    return True


def save_active_users(plugin, *, force_trim: bool = False) -> None:
    now = time.time()
    max_total = _get_active_users_trim_limit(plugin)
    current_total = getattr(plugin, "_active_user_count", None)
    if not isinstance(current_total, int) or current_total < 0:
        current_total = _recount_active_users(plugin)

    if current_total > max_total:
        last_trim_at = float(getattr(plugin, "_active_users_last_trim_at", 0.0) or 0.0)
        trim_interval = getattr(
            plugin,
            "_active_users_trim_interval_seconds",
            ACTIVE_USERS_TRIM_INTERVAL_SECONDS,
        )
        overflow_limit = _get_active_users_trim_overflow(plugin, max_total)
        try:
            trim_interval = max(0, int(trim_interval))
        except Exception:
            trim_interval = ACTIVE_USERS_TRIM_INTERVAL_SECONDS

        if (
            force_trim
            or current_total - max_total >= overflow_limit
            or now - last_trim_at >= trim_interval
        ):
            _trim_active_users_to_limit(plugin, max_total=max_total)
            plugin._active_users_last_trim_at = now

    save_json(plugin.active_file, plugin.active_users)


async def send_onebot_message(plugin, event, *, message: list[dict]) -> object:
    assert isinstance(event, AiocqhttpMessageEvent)

    group_id = event.get_group_id()
    if group_id:
        resp = await event.bot.api.call_action(
            "send_group_msg", group_id=int(group_id), message=message
        )
    else:
        resp = await event.bot.api.call_action(
            "send_private_msg",
            user_id=int(event.get_sender_id()),
            message=message,
        )

    message_id = extract_message_id(resp)
    if message_id is None:
        plugin.logger = getattr(plugin, "logger", None)
        if plugin.logger:
            plugin.logger.warning(f"无法解析 send_*_msg 返回的 message_id: {resp!r}")
    return message_id


def schedule_onebot_delete_msg(plugin, client, *, message_id: object) -> None:
    delay = auto_withdraw_delay_seconds(plugin)

    async def _runner():
        await asyncio.sleep(delay)
        try:
            await client.api.call_action("delete_msg", message_id=message_id)
        except Exception as e:
            plugin.logger = getattr(plugin, "logger", None)
            if plugin.logger:
                plugin.logger.warning(f"自动撤回失败: {e}")

    task = asyncio.create_task(_runner())
    plugin._withdraw_tasks.add(task)
    task.add_done_callback(plugin._withdraw_tasks.discard)


def record_active(plugin, event) -> None:
    group_id = event.get_group_id()
    if not group_id or not is_allowed_group(str(group_id), plugin.config):
        return

    user_id, bot_id = str(event.get_sender_id()), str(event.get_self_id())
    if user_id == bot_id or user_id == "0":
        return

    group_key = str(group_id)
    if group_key not in plugin.active_users or not isinstance(
        plugin.active_users.get(group_key), dict
    ):
        plugin.active_users[group_key] = {}

    active_group = plugin.active_users[group_key]
    is_new_user = user_id not in active_group
    active_group[user_id] = time.time()

    if is_new_user:
        current_total = getattr(plugin, "_active_user_count", None)
        if isinstance(current_total, int) and current_total >= 0:
            plugin._active_user_count = current_total + 1
        else:
            plugin._active_user_count = count_active_users(plugin.active_users)

    save_active_users(plugin)


def clean_rbq_stats(plugin) -> None:
    now = time.time()
    thirty_days = 30 * 24 * 3600
    seven_days = 7 * 24 * 3600
    five_days = 5 * 24 * 3600 # 新增 5 天逻辑

    new_stats = {}
    for gid, users in plugin.rbq_stats.items():
        new_users = {}
        active_group = plugin.active_users.get(gid, {})

        for uid, timestamps in users.items():
            # 1. 只保留 30 天内的强娶记录
            valid_ts = [ts for ts in timestamps if now - ts < thirty_days]
            count = len(valid_ts)

            if count == 0:
                continue # 没有记录直接跳过，不加入 new_users

            # 获取最后一次被强娶的时间（用于没查到活跃记录时的兜底判断）
            last_forced_ts = max(valid_ts) if valid_ts else 0
            
            # 活跃状态检查
            is_in_active = uid in active_group
            last_active_ts = active_group.get(uid, 0)

            should_keep = True
            
            if not is_in_active:
                # --- 核心逻辑修改 ---
                if last_active_ts == 0:
                    # 情况 A: active_users 里完全没记录，根据最后一次强娶时间判断
                    # 如果距离最后一次被强娶已经超过 5 天，则清理
                    if now - last_forced_ts > five_days:
                        should_keep = False
                else:
                    # 情况 B: active_users 有记录，但该用户已经一个月没说话了 (原来的逻辑)
                    if count <= 4 and (now - last_active_ts > seven_days):
                        should_keep = False
                # --------------------

            if should_keep:
                new_users[uid] = valid_ts

        if new_users:
            new_stats[gid] = new_users

    plugin.rbq_stats = new_stats
    save_json(plugin.rbq_stats_file, plugin.rbq_stats)


def draw_excluded_users(plugin) -> Set[str]:
    return normalize_user_id_set(plugin.config.get("excluded_users", []))


def force_marry_excluded_users(plugin) -> Set[str]:
    return normalize_user_id_set(plugin.config.get("force_marry_excluded_users", []))


def ensure_today_records(plugin) -> None:
    today = datetime.now().strftime("%Y-%m-%d")
    if plugin.records.get("date") != today:
        plugin.records = {"date": today, "groups": {}}


def get_group_records(plugin, group_id: str) -> list:
    ensure_today_records(plugin)
    if group_id not in plugin.records["groups"]:
        plugin.records["groups"][group_id] = {"records": []}
    return plugin.records["groups"][group_id]["records"]


PROPOSE_COOLDOWN_SECONDS = 3600


def _get_marriage_action_group(plugin, group_id: str) -> dict:
    group_records = plugin.marriage_action_records.get(group_id)
    if not isinstance(group_records, dict):
        group_records = {}
        plugin.marriage_action_records[group_id] = group_records
    return group_records


def _remove_marriage_action_record(plugin, group_id: str, user_id: str) -> None:
    group_records = plugin.marriage_action_records.get(group_id)
    if not isinstance(group_records, dict):
        return

    group_records.pop(user_id, None)
    if not group_records:
        plugin.marriage_action_records.pop(group_id, None)


def get_propose_cooldown_status(plugin, group_id: str, user_id: str) -> dict | None:
    record = plugin.marriage_action_records.get(group_id, {}).get(user_id)
    if not isinstance(record, dict):
        return None

    action = record.get("action")
    expire_at = record.get("expire_at")
    if action != "propose" or not isinstance(expire_at, (int, float)):
        _remove_marriage_action_record(plugin, group_id, user_id)
        return None

    remaining = expire_at - time.time()
    if remaining <= 0:
        _remove_marriage_action_record(plugin, group_id, user_id)
        return None

    return {
        "action": "propose",
        "start_at": record.get("start_at"),
        "expire_at": expire_at,
        "remaining": remaining,
        "role": record.get("role"),
        "related_user_id": record.get("related_user_id"),
    }


def set_propose_cooldown(
    plugin,
    group_id: str,
    user_id: str,
    *,
    related_user_id: str,
    role: str,
    now: float | None = None,
) -> None:
    start_at = time.time() if now is None else now
    group_records = _get_marriage_action_group(plugin, group_id)
    group_records[user_id] = {
        "action": "propose",
        "start_at": start_at,
        "expire_at": start_at + PROPOSE_COOLDOWN_SECONDS,
        "related_user_id": related_user_id,
        "role": role,
    }


def get_force_marry_cd_days(plugin) -> int:
    raw = plugin.config.get("force_marry_cd", 3)
    try:
        cd_days = int(raw)
    except Exception:
        cd_days = 3
    return max(0, cd_days)


def get_force_marry_cooldown_status(plugin, group_id: str, user_id: str) -> dict | None:
    last_time = plugin.forced_records.setdefault(group_id, {}).get(user_id)
    if not isinstance(last_time, (int, float)):
        return None

    last_dt = datetime.fromtimestamp(last_time)
    cd_days = get_force_marry_cd_days(plugin)
    last_midnight = datetime.combine(last_dt.date(), datetime.min.time())
    reset_dt = last_midnight + timedelta(days=cd_days)
    try:
        reset_ts = reset_dt.timestamp()
    except (OSError, OverflowError):
        reset_ts = 0

    remaining = reset_ts - time.time()
    if remaining <= 0:
        return None

    return {
        "action": "force_marry",
        "last_time": last_time,
        "reset_at": reset_ts,
        "reset_dt": reset_dt,
        "remaining": remaining,
        "cd_days": cd_days,
    }


def auto_set_other_half_enabled(plugin) -> bool:
    return bool(plugin.config.get("auto_set_other_half", False))


def auto_withdraw_enabled(plugin) -> bool:
    return bool(plugin.config.get("auto_withdraw_enabled", False))


def auto_withdraw_delay_seconds(plugin) -> int:
    raw = plugin.config.get("auto_withdraw_delay_seconds", 5)
    try:
        delay = int(raw)
    except Exception:
        delay = 5
    return max(1, delay)


def can_onebot_withdraw(plugin, event) -> bool:
    return auto_withdraw_enabled(plugin) and event.get_platform_name() == "aiocqhttp"


def cleanup_inactive(plugin, group_id: str):
    if group_id not in plugin.active_users:
        return
    now, limit = time.time(), 30 * 24 * 3600
    active_group = plugin.active_users[group_id]
    new_active = {uid: ts for uid, ts in active_group.items() if (now - ts < limit) and uid != "0"}
    if len(active_group) != len(new_active):
        removed_count = len(active_group) - len(new_active)
        if new_active:
            plugin.active_users[group_id] = new_active
        else:
            plugin.active_users.pop(group_id, None)

        current_total = getattr(plugin, "_active_user_count", None)
        if isinstance(current_total, int) and current_total >= 0:
            plugin._active_user_count = max(0, current_total - removed_count)
        else:
            plugin._active_user_count = count_active_users(plugin.active_users)

        save_active_users(plugin)
