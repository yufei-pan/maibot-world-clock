"""世界时钟：时区解析、格式化与 planner/replyer 消息注入。

纯逻辑模块，不依赖 Host / SDK 运行时。
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, MutableMapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

PLANNER_TIME_RE = re.compile(r"^时间：(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})$")
REPLYER_TIME_PREFIX_RE = re.compile(
    r"^当前时间：(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?:\n|$)"
)

VALID_MODES = frozenset({"replace", "append"})
VALID_ON_NO_MATCH = frozenset({"warn_and_append", "warn_only", "error"})

_DEFAULT_LOCALTIME = Path("/etc/localtime")


def normalize_zone_key(name: str) -> str:
    """归一化为 ZoneInfo 可加载的键；UTC 族统一为 Etc/UTC。"""

    stripped = str(name or "").strip()
    if not stripped:
        raise ValueError("时区名不能为空")
    if stripped in {"UTC", "Etc/UTC"}:
        return "Etc/UTC"
    return stripped


def display_zone_name(name: str) -> str:
    """渲染用时区名；UTC 族显示为 UTC。"""

    stripped = str(name or "").strip()
    if stripped in {"UTC", "Etc/UTC"}:
        return "UTC"
    return stripped


def load_zone(name: str) -> ZoneInfo:
    """加载 IANA 时区；失败时抛出包含原名的 ValueError。"""

    key = normalize_zone_key(name)
    try:
        return ZoneInfo(key)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"无效的 IANA 时区：{name}") from exc


def resolve_primary_timezone(
    override: str | None,
    *,
    environ: Mapping[str, str] | None = None,
    localtime_path: Path | None = None,
) -> str:
    """按配置覆盖 → TZ → /etc/localtime 顺序解析本地 IANA 时区名。"""

    if override is not None and str(override).strip():
        name = str(override).strip()
        load_zone(name)
        return display_zone_name(name) if normalize_zone_key(name) == "Etc/UTC" else name

    env = environ if environ is not None else os.environ
    tz_env = str(env.get("TZ") or "").strip()
    if tz_env:
        # TZ 有时写成 :/etc/localtime，忽略该形式，继续走 symlink
        if not tz_env.startswith(":"):
            try:
                load_zone(tz_env)
                return "UTC" if normalize_zone_key(tz_env) == "Etc/UTC" else tz_env
            except ValueError:
                pass

    path = localtime_path if localtime_path is not None else _DEFAULT_LOCALTIME
    resolved = _iana_from_localtime_symlink(path)
    if resolved is not None:
        load_zone(resolved)
        return resolved

    raise ValueError(
        "无法自动解析进程时区，请在配置中设置 clock.primary_timezone（IANA，例如 America/Los_Angeles）"
    )


def _iana_from_localtime_symlink(localtime_path: Path) -> str | None:
    """若 localtime 是指向 zoneinfo 树的符号链接，返回 IANA 名。"""

    try:
        if not localtime_path.is_symlink():
            return None
        target = localtime_path.resolve()
    except OSError:
        return None

    parts = target.parts
    for index, part in enumerate(parts):
        if part in {"zoneinfo", "tzdata"} and index + 1 < len(parts):
            candidate = "/".join(parts[index + 1 :])
            if candidate:
                return candidate
    return None


def dedupe_world_timezones(primary: str, world: Sequence[str]) -> list[str]:
    """去掉与 primary 相同的世界时区，保留配置顺序；同 key 只保留首次出现。"""

    primary_key = normalize_zone_key(primary)
    seen: set[str] = set()
    result: list[str] = []
    for item in world:
        name = str(item).strip()
        if not name:
            continue
        key = normalize_zone_key(name)
        if key == primary_key:
            continue
        if key in seen:
            continue
        seen.add(key)
        result.append("UTC" if key == "Etc/UTC" else name)
    return result


def wall_to_instant(wall: datetime, primary: str) -> datetime:
    """把无时区墙钟时间解释为 primary 时区下的瞬时。"""

    if wall.tzinfo is not None:
        raise ValueError("墙钟时间必须是 naive datetime")
    zone = load_zone(primary)
    return wall.replace(tzinfo=zone)


def format_line(instant: datetime, zone_name: str) -> str:
    """单行：`- IANA (ABBR): YYYY-MM-DD HH:MM:SS`。"""

    zone = load_zone(zone_name)
    local = instant.astimezone(zone)
    abbr = local.tzname() or display_zone_name(zone_name)
    stamp = local.strftime("%Y-%m-%d %H:%M:%S")
    return f"- {display_zone_name(zone_name)} ({abbr}): {stamp}"


def format_clock_block(instant: datetime, primary: str, world: Sequence[str]) -> str:
    """渲染本地时间 +（可选）世界时间块。"""

    if instant.tzinfo is None:
        raise ValueError("instant 必须是 aware datetime")

    lines = ["本地时间：", format_line(instant, primary)]
    world_names = dedupe_world_timezones(primary, world)
    if world_names:
        lines.append("世界时间：")
        for name in world_names:
            lines.append(format_line(instant, name))
    return "\n".join(lines)


def parse_planner_time_message(text: str) -> datetime | None:
    """若整段文本是 Host planner 时间消息，返回 naive 墙钟时间。"""

    match = PLANNER_TIME_RE.match(str(text).strip())
    if match is None:
        return None
    return datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")


def parse_replyer_time_prefix(text: str) -> tuple[datetime, str] | None:
    """若文本以 Host replyer 当前时间开头，返回 (naive 墙钟, 其余文本)。"""

    raw = str(text)
    match = REPLYER_TIME_PREFIX_RE.match(raw)
    if match is None:
        return None
    wall = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
    remainder = raw[match.end() :]
    # 丢掉紧跟的一个多余换行，使 remainder 以内容或空串开始；保留后续 \n\n 分段语义由调用方用 \n\n 拼接
    if remainder.startswith("\n"):
        remainder = remainder[1:]
    return wall, remainder


def _message_text(content: Any) -> str | None:
    if isinstance(content, str):
        return content
    return None


def _set_message_text(message: MutableMapping[str, Any], text: str) -> None:
    message["content"] = text


def _iter_user_message_targets(messages: list[Any]) -> list[tuple[int, MutableMapping[str, Any], str]]:
    """收集 role=user 且 content 为纯字符串的消息。"""

    targets: list[tuple[int, MutableMapping[str, Any], str]] = []
    for index, item in enumerate(messages):
        if not isinstance(item, MutableMapping):
            continue
        if item.get("role") != "user":
            continue
        text = _message_text(item.get("content"))
        if text is None:
            # 多模态：尝试改写首个 text part
            content = item.get("content")
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, MutableMapping) and part.get("type") == "text":
                        part_text = part.get("text")
                        if isinstance(part_text, str):
                            targets.append((index, part, part_text))  # type: ignore[arg-type]
                        break
            continue
        targets.append((index, item, text))
    return targets


def _append_user_message(messages: list[Any], text: str) -> list[Any]:
    out = list(messages)
    out.append({"role": "user", "content": text})
    return out


def _resolve_now(now: datetime | None, primary: str) -> datetime:
    if now is not None:
        if now.tzinfo is None:
            return wall_to_instant(now, primary)
        return now
    return datetime.now(tz=load_zone(primary))


def apply_planner_messages(
    messages: list[Any],
    *,
    primary: str,
    world: Sequence[str],
    mode: str,
    on_no_match: str,
    now: datetime | None = None,
) -> tuple[list[Any], str | None]:
    """对 planner 请求 messages 做 replace/append。返回 (新列表, 警告或 None)。"""

    if mode not in VALID_MODES:
        raise ValueError(f"无效的 mode：{mode}")
    if on_no_match not in VALID_ON_NO_MATCH:
        raise ValueError(f"无效的 on_no_match：{on_no_match}")

    if mode == "append":
        block = format_clock_block(_resolve_now(now, primary), primary, world)
        return _append_user_message(messages, block), None

    out = [dict(item) if isinstance(item, MutableMapping) else item for item in messages]
    matched = 0
    for index, item in enumerate(out):
        if not isinstance(item, MutableMapping) or item.get("role") != "user":
            continue
        content = item.get("content")
        if isinstance(content, str):
            wall = parse_planner_time_message(content)
            if wall is None:
                continue
            instant = wall_to_instant(wall, primary)
            item["content"] = format_clock_block(instant, primary, world)
            matched += 1
            continue
        if isinstance(content, list):
            new_parts = list(content)
            for part_index, part in enumerate(new_parts):
                if not isinstance(part, MutableMapping) or part.get("type") != "text":
                    continue
                part_text = part.get("text")
                if not isinstance(part_text, str):
                    break
                wall = parse_planner_time_message(part_text)
                if wall is None:
                    break
                instant = wall_to_instant(wall, primary)
                new_part = dict(part)
                new_part["text"] = format_clock_block(instant, primary, world)
                new_parts[part_index] = new_part
                item["content"] = new_parts
                matched += 1
                break

    if matched > 0:
        return out, None

    warning = "世界时钟：replace 模式未匹配到 Host planner 时间消息（期望整段为「时间：YYYY-MM-DD HH:MM:SS」）"
    if on_no_match == "error":
        raise RuntimeError(warning)
    if on_no_match == "warn_only":
        return out, warning
    block = format_clock_block(_resolve_now(now, primary), primary, world)
    return _append_user_message(out, block), warning


def apply_replyer_messages(
    messages: list[Any],
    *,
    primary: str,
    world: Sequence[str],
    mode: str,
    on_no_match: str,
    now: datetime | None = None,
) -> tuple[list[Any], str | None]:
    """对 replyer 请求 messages 做 replace/append。"""

    if mode not in VALID_MODES:
        raise ValueError(f"无效的 mode：{mode}")
    if on_no_match not in VALID_ON_NO_MATCH:
        raise ValueError(f"无效的 on_no_match：{on_no_match}")

    out = [dict(item) if isinstance(item, MutableMapping) else item for item in messages]

    def _rewrite_text(text: str) -> str | None:
        parsed = parse_replyer_time_prefix(text)
        if parsed is None:
            return None
        wall, remainder = parsed
        instant = wall_to_instant(wall, primary)
        block = format_clock_block(instant, primary, world)
        if remainder:
            return f"{block}\n\n{remainder}" if not remainder.startswith("\n") else f"{block}\n{remainder}"
        return block

    if mode == "append":
        block = format_clock_block(_resolve_now(now, primary), primary, world)
        for item in out:
            if not isinstance(item, MutableMapping) or item.get("role") != "user":
                continue
            content = item.get("content")
            if isinstance(content, str) and parse_replyer_time_prefix(content) is not None:
                item["content"] = f"{content}\n\n{block}"
                return out, None
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, MutableMapping) and part.get("type") == "text":
                        part_text = part.get("text")
                        if isinstance(part_text, str) and parse_replyer_time_prefix(part_text) is not None:
                            part["text"] = f"{part_text}\n\n{block}"
                            return out, None
                        break
        return _append_user_message(out, block), None

    matched = 0
    for item in out:
        if not isinstance(item, MutableMapping) or item.get("role") != "user":
            continue
        content = item.get("content")
        if isinstance(content, str):
            rewritten = _rewrite_text(content)
            if rewritten is None:
                continue
            item["content"] = rewritten
            matched += 1
            continue
        if isinstance(content, list):
            new_parts = list(content)
            for part_index, part in enumerate(new_parts):
                if not isinstance(part, MutableMapping) or part.get("type") != "text":
                    continue
                part_text = part.get("text")
                if not isinstance(part_text, str):
                    break
                rewritten = _rewrite_text(part_text)
                if rewritten is None:
                    break
                new_part = dict(part)
                new_part["text"] = rewritten
                new_parts[part_index] = new_part
                item["content"] = new_parts
                matched += 1
                break

    if matched > 0:
        return out, None

    warning = "世界时钟：replace 模式未匹配到 Host replyer 时间前缀（期望以「当前时间：YYYY-MM-DD HH:MM:SS」开头）"
    if on_no_match == "error":
        raise RuntimeError(warning)
    if on_no_match == "warn_only":
        return out, warning
    block = format_clock_block(_resolve_now(now, primary), primary, world)
    return _append_user_message(out, block), warning
