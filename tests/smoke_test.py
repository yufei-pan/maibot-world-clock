"""离线冒烟测试：不依赖 MaiBot Host。

运行方式（在插件根目录）：
    PYTHONPATH=../maibot-plugin-sdk python tests/smoke_test.py
"""

from __future__ import annotations

import json
import sys
import tomllib
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))
sys.path.insert(0, str(PLUGIN_DIR.parent / "maibot-plugin-sdk"))

import plugin as world_clock_plugin  # noqa: E402
from clock import apply_planner_messages, format_clock_block, wall_to_instant  # noqa: E402


def test_manifest() -> None:
    manifest = json.loads((PLUGIN_DIR / "_manifest.json").read_text(encoding="utf-8"))
    assert manifest["id"] == "com.0-hz.world-clock"
    assert manifest["name"] == "世界时钟（World Clock）"
    assert manifest["manifest_version"] == 2
    assert "maisaka" not in "".join(manifest.get("capabilities") or [])
    print("ok: manifest")


def test_default_toml_matches_version() -> None:
    data = tomllib.loads((PLUGIN_DIR / "config.default.toml").read_text(encoding="utf-8"))
    assert data["plugin"]["config_version"] == world_clock_plugin.CURRENT_CONFIG_VERSION
    assert data["clock"]["world_timezones"] == ["Asia/Shanghai", "UTC"]
    print("ok: config.default.toml")


def test_config_model_and_settings() -> None:
    cfg = world_clock_plugin.WorldClockConfig()
    assert cfg.plugin.config_version == world_clock_plugin.CURRENT_CONFIG_VERSION
    settings = world_clock_plugin.build_clock_settings(
        world_clock_plugin.WorldClockConfig.model_validate(
            {
                "plugin": {"enabled": True, "config_version": "1.0.0"},
                "clock": {
                    "primary_timezone": "America/Los_Angeles",
                    "world_timezones": ["Asia/Shanghai", "UTC"],
                    "mode": "replace",
                    "on_no_match": "warn_and_append",
                },
            }
        )
    )
    assert settings.primary_timezone == "America/Los_Angeles"
    assert settings.world_timezones == ("Asia/Shanghai", "UTC")
    print("ok: config model + settings")


def test_bad_iana_rejected() -> None:
    try:
        world_clock_plugin._normalize_world_clock_config(
            {
                "plugin": {"enabled": True, "config_version": "1.0.0"},
                "clock": {"world_timezones": ["Not/AZone"]},
            },
            world_clock_plugin.WorldClockConfig().model_dump(mode="python"),
        )
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "Not/AZone" in str(exc)
    print("ok: bad IANA rejected")


def test_create_plugin_factory() -> None:
    instance = world_clock_plugin.create_plugin()
    assert isinstance(instance, world_clock_plugin.WorldClockPlugin)
    assert instance.config_model is world_clock_plugin.WorldClockConfig
    print("ok: create_plugin")


def test_end_to_end_format() -> None:
    instant = wall_to_instant(datetime(2026, 7, 28, 23, 24, 0), "America/Los_Angeles")
    text = format_clock_block(instant, "America/Los_Angeles", ["Asia/Shanghai", "UTC"])
    assert "本地时间：" in text and "世界时间：" in text
    messages = [{"role": "user", "content": "时间：2026-07-28 23:24:00"}]
    out, warn = apply_planner_messages(
        messages,
        primary="America/Los_Angeles",
        world=["Asia/Shanghai", "UTC"],
        mode="replace",
        on_no_match="warn_only",
        now=datetime(2099, 1, 1, tzinfo=ZoneInfo("America/Los_Angeles")),
    )
    assert warn is None
    assert out[0]["content"] == text
    print("ok: end-to-end inject")


def main() -> None:
    test_manifest()
    test_default_toml_matches_version()
    test_config_model_and_settings()
    test_bad_iana_rejected()
    test_create_plugin_factory()
    test_end_to_end_format()
    print("all smoke tests passed")


if __name__ == "__main__":
    main()
