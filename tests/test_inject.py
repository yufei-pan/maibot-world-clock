"""planner / replyer 消息注入单测。"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))

from clock import apply_planner_messages, apply_replyer_messages  # noqa: E402

PRIMARY = "America/Los_Angeles"
WORLD = ["Asia/Shanghai", "UTC"]


def test_planner_replace_preserves_historic_instant() -> None:
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "时间：2026-07-20 10:00:00"},
        {"role": "user", "content": "时间：2026-07-28 23:24:00"},
    ]
    frozen_now = datetime(2099, 1, 1, 0, 0, 0, tzinfo=ZoneInfo(PRIMARY))
    out, warn = apply_planner_messages(
        messages,
        primary=PRIMARY,
        world=WORLD,
        mode="replace",
        on_no_match="warn_only",
        now=frozen_now,
    )
    assert warn is None
    assert "2026-07-20 10:00:00" in out[1]["content"]
    assert "2099" not in out[1]["content"]
    assert "2026-07-28 23:24:00" in out[2]["content"]
    assert out[1]["content"].startswith("本地时间：")


def test_planner_append_leaves_host_and_adds_now() -> None:
    messages = [{"role": "user", "content": "时间：2026-07-28 23:24:00"}]
    frozen_now = datetime(2026, 7, 28, 23, 30, 0, tzinfo=ZoneInfo(PRIMARY))
    out, _ = apply_planner_messages(
        messages,
        primary=PRIMARY,
        world=WORLD,
        mode="append",
        on_no_match="warn_only",
        now=frozen_now,
    )
    assert out[0]["content"] == "时间：2026-07-28 23:24:00"
    assert out[-1]["content"].startswith("本地时间：")
    assert "23:30:00" in out[-1]["content"]


def test_planner_replace_no_match_warn_and_append() -> None:
    messages = [{"role": "user", "content": "hello"}]
    frozen_now = datetime(2026, 7, 28, 23, 30, 0, tzinfo=ZoneInfo(PRIMARY))
    out, warn = apply_planner_messages(
        messages,
        primary=PRIMARY,
        world=WORLD,
        mode="replace",
        on_no_match="warn_and_append",
        now=frozen_now,
    )
    assert warn is not None
    assert out[-1]["content"].startswith("本地时间：")


def test_planner_replace_no_match_warn_only() -> None:
    messages = [{"role": "user", "content": "hello"}]
    out, warn = apply_planner_messages(
        messages,
        primary=PRIMARY,
        world=WORLD,
        mode="replace",
        on_no_match="warn_only",
        now=datetime(2026, 7, 28, 23, 30, 0, tzinfo=ZoneInfo(PRIMARY)),
    )
    assert warn is not None
    assert len(out) == 1
    assert out[0]["content"] == "hello"


def test_planner_replace_no_match_error() -> None:
    with pytest.raises(RuntimeError, match="未匹配"):
        apply_planner_messages(
            [{"role": "user", "content": "hello"}],
            primary=PRIMARY,
            world=WORLD,
            mode="replace",
            on_no_match="error",
        )


def test_replyer_replace_keeps_trailing_sections() -> None:
    content = "当前时间：2026-07-28 23:24:00\n\n目标消息：你好"
    messages = [{"role": "user", "content": content}]
    out, _ = apply_replyer_messages(
        messages,
        primary=PRIMARY,
        world=WORLD,
        mode="replace",
        on_no_match="warn_only",
        now=None,
    )
    assert out[0]["content"].startswith("本地时间：")
    assert out[0]["content"].endswith("目标消息：你好")
    assert "当前时间：" not in out[0]["content"]
    assert "2099" not in out[0]["content"]


def test_replyer_append_extends_current_time_message() -> None:
    content = "当前时间：2026-07-28 23:24:00\n\n目标消息：你好"
    frozen_now = datetime(2026, 7, 28, 23, 30, 0, tzinfo=ZoneInfo(PRIMARY))
    out, _ = apply_replyer_messages(
        [{"role": "user", "content": content}],
        primary=PRIMARY,
        world=WORLD,
        mode="append",
        on_no_match="warn_only",
        now=frozen_now,
    )
    assert out[0]["content"].startswith("当前时间：2026-07-28 23:24:00")
    assert "本地时间：" in out[0]["content"]
    assert "23:30:00" in out[0]["content"]
