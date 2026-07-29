"""世界时钟格式化与时区解析单测。"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))

import plugin as world_clock  # noqa: E402

dedupe_world_timezones = world_clock.dedupe_world_timezones
display_zone_name = world_clock.display_zone_name
format_clock_block = world_clock.format_clock_block
load_zone = world_clock.load_zone
normalize_zone_key = world_clock.normalize_zone_key
resolve_primary_timezone = world_clock.resolve_primary_timezone
wall_to_instant = world_clock.wall_to_instant


def test_normalize_utc() -> None:
    assert normalize_zone_key("UTC") == "Etc/UTC"
    assert normalize_zone_key("Etc/UTC") == "Etc/UTC"
    assert display_zone_name("UTC") == "UTC"
    assert display_zone_name("Etc/UTC") == "UTC"


def test_dedupe_primary_out_of_world() -> None:
    assert dedupe_world_timezones("Asia/Shanghai", ["Asia/Shanghai", "UTC"]) == ["UTC"]
    assert dedupe_world_timezones("UTC", ["Asia/Shanghai", "Etc/UTC"]) == ["Asia/Shanghai"]


def test_format_block_with_world() -> None:
    instant = wall_to_instant(datetime(2026, 7, 28, 23, 24, 0), "America/Los_Angeles")
    text = format_clock_block(instant, "America/Los_Angeles", ["Asia/Shanghai", "UTC"])
    assert text.startswith("本地时间：\n- America/Los_Angeles (PDT): 2026-07-28 23:24:00")
    assert "世界时间：\n- Asia/Shanghai (CST): 2026-07-29 14:24:00\n- UTC (UTC): 2026-07-29 06:24:00" in text


def test_format_omits_world_when_empty_after_dedupe() -> None:
    instant = wall_to_instant(datetime(2026, 7, 29, 14, 24, 0), "Asia/Shanghai")
    text = format_clock_block(instant, "Asia/Shanghai", ["Asia/Shanghai"])
    assert text == "本地时间：\n- Asia/Shanghai (CST): 2026-07-29 14:24:00"
    assert "世界时间" not in text


def test_dst_abbreviation_changes() -> None:
    winter = wall_to_instant(datetime(2026, 1, 15, 12, 0, 0), "America/Los_Angeles")
    summer = wall_to_instant(datetime(2026, 7, 15, 12, 0, 0), "America/Los_Angeles")
    assert "(PST)" in format_clock_block(winter, "America/Los_Angeles", [])
    assert "(PDT)" in format_clock_block(summer, "America/Los_Angeles", [])


def test_load_zone_rejects_bad() -> None:
    try:
        load_zone("Not/AZone")
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "Not/AZone" in str(exc)


def test_resolve_primary_from_override() -> None:
    assert resolve_primary_timezone("Europe/Berlin") == "Europe/Berlin"


def test_resolve_primary_from_tz_env() -> None:
    assert resolve_primary_timezone(None, environ={"TZ": "Asia/Tokyo"}) == "Asia/Tokyo"


def test_resolve_primary_from_localtime_symlink(tmp_path: Path) -> None:
    zoneinfo = tmp_path / "zoneinfo" / "America" / "Los_Angeles"
    zoneinfo.parent.mkdir(parents=True)
    zoneinfo.write_text("tzdata", encoding="utf-8")
    localtime = tmp_path / "localtime"
    localtime.symlink_to(zoneinfo)
    assert resolve_primary_timezone(None, environ={}, localtime_path=localtime) == "America/Los_Angeles"
