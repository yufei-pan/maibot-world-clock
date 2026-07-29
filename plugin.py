"""世界时钟（World Clock）插件。

在 Maisaka planner / replyer 请求模型前，把 Host 注入的无时区时间提示
改写（或追加）为带 IANA 与缩写的「本地时间 / 世界时间」块。
"""

from __future__ import annotations

import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import UnionType
from typing import Any, Union, get_args, get_origin

from maibot_sdk import CONFIG_RELOAD_SCOPE_SELF, Field, HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.config import validate_plugin_config
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

from clock import (
    VALID_MODES,
    VALID_ON_NO_MATCH,
    apply_planner_messages,
    apply_replyer_messages,
    load_zone,
    resolve_primary_timezone,
)

CURRENT_CONFIG_VERSION = "1.0.0"
SHIPPED_CONFIG_TEMPLATE_NAME = "config.default.toml"
HOOK_TIMEOUT_MS = 5000

DEFAULT_WORLD_TIMEZONES = ["Asia/Shanghai", "UTC"]
DEFAULT_MODE = "replace"
DEFAULT_ON_NO_MATCH = "warn_and_append"


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
    # 以默认结构为骨架覆盖用户值（保留未知节会被 model 丢掉，符合预期）
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
        # 再走 SDK 默认补齐
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
