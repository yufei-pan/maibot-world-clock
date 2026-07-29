"""世界时钟（World Clock）插件。

在 Maisaka planner / replyer 请求模型前，把 Host 注入的无时区时间提示
改写（或追加）为带 IANA 与缩写的「本地时间 / 世界时间」块。

全部逻辑放在本文件：Host Runner 只保证能加载 plugin.py，不能依赖同目录子模块 import。
"""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import UnionType
from typing import Any, Union, get_args, get_origin
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from maibot_sdk import CONFIG_RELOAD_SCOPE_SELF, Field, HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.config import validate_plugin_config
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

CURRENT_CONFIG_VERSION = "1.0.0"
SHIPPED_CONFIG_TEMPLATE_NAME = "config.default.toml"
HOOK_TIMEOUT_MS = 5000

DEFAULT_WORLD_TIMEZONES = ["Asia/Shanghai", "UTC"]
DEFAULT_MODE = "replace"
DEFAULT_ON_NO_MATCH = "warn_and_append"

PLANNER_TIME_RE = re.compile(r"^时间：(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})$")
REPLYER_TIME_PREFIX_RE = re.compile(
    r"^当前时间：(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?:\n|$)"
)

VALID_MODES = frozenset({"replace", "append"})
VALID_ON_NO_MATCH = frozenset({"warn_and_append", "warn_only", "error"})

_DEFAULT_LOCALTIME = Path("/etc/localtime")


# --------------------------------------------------------------------------- #
# 时区解析与格式化
# --------------------------------------------------------------------------- #


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
    if remainder.startswith("\n"):
        remainder = remainder[1:]
    return wall, remainder


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
    for item in out:
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


# --------------------------------------------------------------------------- #
# 配置模型
# --------------------------------------------------------------------------- #


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default=CURRENT_CONFIG_VERSION, description="配置版本")


class ClockSectionConfig(PluginConfigBase):
    """世界时钟配置。"""

    __ui_label__ = "世界时钟"
    __ui_icon__ = "clock"
    __ui_order__ = 1

    primary_timezone: str | None = Field(
        default=None,
        json_schema_extra={
            "label": "本地时区覆盖",
            "placeholder": "America/Los_Angeles",
            "i18n": {"zh-CN": {"label": "本地时区覆盖", "placeholder": "America/Los_Angeles"}},
        },
        description="本地时区（IANA）。留空则自动解析进程时区（TZ 或 /etc/localtime）。",
    )
    world_timezones: list[str] = Field(
        default_factory=lambda: list(DEFAULT_WORLD_TIMEZONES),
        json_schema_extra={
            "label": "世界时区列表",
            "i18n": {"zh-CN": {"label": "世界时区列表"}},
        },
        description="额外展示的世界时区（IANA）。与本地相同时自动去掉；去重后为空则不渲染「世界时间」块。",
    )
    mode: str = Field(
        default=DEFAULT_MODE,
        json_schema_extra={
            "label": "注入模式",
            "i18n": {"zh-CN": {"label": "注入模式"}},
        },
        description="replace=改写 Host 时间字符串；append=不改写只追加（Host 文案变更时的兼容模式）。",
    )
    on_no_match: str = Field(
        default=DEFAULT_ON_NO_MATCH,
        json_schema_extra={
            "label": "未匹配时行为",
            "i18n": {"zh-CN": {"label": "未匹配时行为"}},
        },
        description="replace 模式下未找到 Host 时间字符串时：warn_and_append / warn_only / error。",
    )


class WorldClockConfig(PluginConfigBase):
    """插件根配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    clock: ClockSectionConfig = Field(default_factory=ClockSectionConfig)


@dataclass(frozen=True)
class ClockSettings:
    """运行期时钟参数快照。"""

    enabled: bool
    primary_timezone: str
    world_timezones: tuple[str, ...]
    mode: str
    on_no_match: str


# --------------------------------------------------------------------------- #
# 配置辅助
# --------------------------------------------------------------------------- #


def _is_optional_annotation(annotation: Any) -> bool:
    origin = get_origin(annotation)
    if origin is Union or origin is UnionType:
        return type(None) in get_args(annotation)
    return False


def _coerce_webui_blank_optionals(config: dict[str, Any], model: type[PluginConfigBase]) -> dict[str, Any]:
    """把 WebUI 写回的空字符串可选字段转为 None。"""

    for field_name, field_info in model.model_fields.items():
        if field_name not in config:
            continue
        annotation = field_info.annotation
        value = config[field_name]
        if isinstance(value, dict) and isinstance(annotation, type) and issubclass(annotation, PluginConfigBase):
            config[field_name] = _coerce_webui_blank_optionals(value, annotation)
            continue
        nested = get_origin(annotation)
        if nested is None and isinstance(annotation, type) and issubclass(annotation, PluginConfigBase) and isinstance(value, dict):
            config[field_name] = _coerce_webui_blank_optionals(value, annotation)
            continue
        if _is_optional_annotation(annotation) and value == "":
            config[field_name] = None
    return config


def _dump_config_for_persist(config: Mapping[str, Any] | WorldClockConfig) -> dict[str, Any]:
    if isinstance(config, WorldClockConfig):
        return config.model_dump(mode="python")
    return dict(config)


def _normalize_world_clock_config(
    config_data: Mapping[str, Any],
    default_config: Mapping[str, Any],
) -> tuple[dict[str, Any], bool, list[str]]:
    """补齐缺失字段、校验枚举与 IANA，并 bump config_version。"""

    notes: list[str] = []
    merged = validate_plugin_config(WorldClockConfig, {**default_config, **dict(config_data)}).model_dump(
        mode="python"
    )
    user = dict(config_data)
    plugin_user = user.get("plugin") if isinstance(user.get("plugin"), Mapping) else {}
    clock_user = user.get("clock") if isinstance(user.get("clock"), Mapping) else {}
    merged["plugin"] = {**merged["plugin"], **dict(plugin_user)}
    merged["clock"] = {**merged["clock"], **dict(clock_user)}

    changed = False
    if merged["plugin"].get("config_version") != CURRENT_CONFIG_VERSION:
        merged["plugin"]["config_version"] = CURRENT_CONFIG_VERSION
        changed = True
        notes.append(f"config_version → {CURRENT_CONFIG_VERSION}")

    clock = merged["clock"]
    mode = str(clock.get("mode") or DEFAULT_MODE).strip()
    if mode not in VALID_MODES:
        raise ValueError(f"clock.mode 无效：{mode!r}，允许值：{sorted(VALID_MODES)}")
    clock["mode"] = mode

    on_no_match = str(clock.get("on_no_match") or DEFAULT_ON_NO_MATCH).strip()
    if on_no_match not in VALID_ON_NO_MATCH:
        raise ValueError(
            f"clock.on_no_match 无效：{on_no_match!r}，允许值：{sorted(VALID_ON_NO_MATCH)}"
        )
    clock["on_no_match"] = on_no_match

    primary = clock.get("primary_timezone")
    if primary is not None and str(primary).strip():
        load_zone(str(primary).strip())
        clock["primary_timezone"] = str(primary).strip()
    else:
        clock["primary_timezone"] = None

    world_raw = clock.get("world_timezones")
    if world_raw is None:
        world_list = list(DEFAULT_WORLD_TIMEZONES)
    elif isinstance(world_raw, Sequence) and not isinstance(world_raw, (str, bytes)):
        world_list = [str(item).strip() for item in world_raw if str(item).strip()]
    else:
        raise ValueError("clock.world_timezones 必须是字符串列表")
    for name in world_list:
        load_zone(name)
    clock["world_timezones"] = world_list

    merged["clock"] = clock
    return merged, changed, notes


def _ensure_shipped_config_present(plugin_dir: Path) -> bool:
    """若缺少运行期 config.toml，从模板复制。"""

    config_path = plugin_dir / "config.toml"
    template_path = plugin_dir / SHIPPED_CONFIG_TEMPLATE_NAME
    if config_path.exists() or not template_path.exists():
        return False
    shutil.copy2(template_path, config_path)
    return True


def build_clock_settings(config: WorldClockConfig) -> ClockSettings:
    """从强类型配置构建运行快照；解析本地时区并校验。"""

    primary_override = config.clock.primary_timezone
    primary = resolve_primary_timezone(primary_override)
    load_zone(primary)
    world = tuple(str(item).strip() for item in config.clock.world_timezones if str(item).strip())
    for name in world:
        load_zone(name)
    mode = str(config.clock.mode or DEFAULT_MODE).strip()
    if mode not in VALID_MODES:
        raise ValueError(f"clock.mode 无效：{mode!r}")
    on_no_match = str(config.clock.on_no_match or DEFAULT_ON_NO_MATCH).strip()
    if on_no_match not in VALID_ON_NO_MATCH:
        raise ValueError(f"clock.on_no_match 无效：{on_no_match!r}")
    return ClockSettings(
        enabled=bool(config.plugin.enabled),
        primary_timezone=primary,
        world_timezones=world,
        mode=mode,
        on_no_match=on_no_match,
    )


# --------------------------------------------------------------------------- #
# 插件
# --------------------------------------------------------------------------- #


class WorldClockPlugin(MaiBotPlugin):
    """世界时钟插件主体。"""

    config_model = WorldClockConfig

    def __init__(self) -> None:
        super().__init__()
        self._plugin_dir = Path(__file__).resolve().parent
        self._settings: ClockSettings | None = None

    def normalize_plugin_config(
        self, config_data: Mapping[str, Any] | None
    ) -> tuple[dict[str, Any], bool]:
        sanitized = _coerce_webui_blank_optionals(dict(config_data or {}), WorldClockConfig)
        default_config = WorldClockConfig().model_dump(mode="python")
        normalized, changed, _notes = _normalize_world_clock_config(sanitized, default_config)
        sdk_normalized, sdk_changed = super().normalize_plugin_config(normalized)
        persistable = _dump_config_for_persist(sdk_normalized)
        return persistable, changed or sdk_changed

    async def on_load(self) -> None:
        self._refresh_settings()
        settings = self._settings
        if settings is None:
            raise RuntimeError("世界时钟配置快照未初始化")
        self.ctx.logger.info(
            "世界时钟已加载：enabled=%s, primary=%s, world=%s, mode=%s, on_no_match=%s",
            settings.enabled,
            settings.primary_timezone,
            list(settings.world_timezones),
            settings.mode,
            settings.on_no_match,
        )

    async def on_unload(self) -> None:
        self.ctx.logger.info("世界时钟已卸载")

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        del config_data
        if scope == CONFIG_RELOAD_SCOPE_SELF:
            self._refresh_settings()
            self.ctx.logger.info("世界时钟配置已更新: version=%s", version)

    def _refresh_settings(self) -> None:
        self._settings = build_clock_settings(self.config)

    def _require_settings(self) -> ClockSettings:
        if self._settings is None:
            self._refresh_settings()
        if self._settings is None:
            raise RuntimeError("世界时钟配置快照未初始化")
        return self._settings

    @HookHandler(
        "maisaka.planner.before_request",
        name="world_clock_planner",
        description="将规划器时间提示改写为带时区的本地/世界时间",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
        timeout_ms=HOOK_TIMEOUT_MS,
        error_policy=ErrorPolicy.SKIP,
    )
    async def on_planner_before_request(self, **kwargs: Any) -> dict[str, Any]:
        settings = self._require_settings()
        if not settings.enabled:
            return {"action": "continue"}
        messages = kwargs.get("messages")
        if not isinstance(messages, list):
            return {"action": "continue"}
        new_messages, warning = apply_planner_messages(
            messages,
            primary=settings.primary_timezone,
            world=settings.world_timezones,
            mode=settings.mode,
            on_no_match=settings.on_no_match,
        )
        if warning:
            self.ctx.logger.warning("%s", warning)
        kwargs["messages"] = new_messages
        return {"action": "continue", "modified_kwargs": kwargs}

    @HookHandler(
        "maisaka.replyer.before_model_request",
        name="world_clock_replyer",
        description="将回复器时间提示改写为带时区的本地/世界时间",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
        timeout_ms=HOOK_TIMEOUT_MS,
        error_policy=ErrorPolicy.SKIP,
    )
    async def on_replyer_before_model_request(self, **kwargs: Any) -> dict[str, Any]:
        settings = self._require_settings()
        if not settings.enabled:
            return {"action": "continue"}
        messages = kwargs.get("messages")
        if not isinstance(messages, list):
            return {"action": "continue"}
        new_messages, warning = apply_replyer_messages(
            messages,
            primary=settings.primary_timezone,
            world=settings.world_timezones,
            mode=settings.mode,
            on_no_match=settings.on_no_match,
        )
        if warning:
            self.ctx.logger.warning("%s", warning)
        kwargs["messages"] = new_messages
        return {"action": "continue", "modified_kwargs": kwargs}


def create_plugin() -> WorldClockPlugin:
    """创建插件实例。"""

    _ensure_shipped_config_present(Path(__file__).resolve().parent)
    return WorldClockPlugin()
