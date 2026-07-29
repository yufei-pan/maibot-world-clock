# maibot-world-clock Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship first-party plugin `maibot-world-clock` that rewrites/appends timezone-aware 本地时间/世界时间 blocks into Maisaka planner and replyer LLM prompts.

**Architecture:** Pure `clock.py` (resolve / parse / format / inject) + `plugin.py` (config + two blocking hooks). Default `mode=replace` rewrites Host `时间：` / `当前时间：` strings using the **parsed** wall time; `mode=append` leaves Host text and adds a now block.

**Tech Stack:** Python ≥3.10, `maibot-plugin-sdk` (`maibot_sdk`), stdlib `zoneinfo` / `datetime` / `re`. No extra PyPI deps.

**Spec:** `docs/superpowers/specs/2026-07-28-maibot-world-clock-design.md`

## Global Constraints

- Plugin-only; do not edit `MaiBot/` or `maibot-plugin-sdk/`
- User-facing text: 简体中文 first
- No silent fallbacks for invalid IANA or formatter bugs; `on_no_match=warn_and_append` is the only intentional pattern-miss fallback
- Manifest id `com.0-hz.world-clock`; author `kes`; SDK range `2.5.1`–`2.99.99`
- Tests: `PYTHONPATH=../maibot-plugin-sdk` from plugin root
- Work in repo `maibot-world-clock/`; commit on a feature branch

## File map

| File | Responsibility |
|---|---|
| `clock.py` | IANA normalize/load, primary resolve, format blocks, parse Host times, inject into message lists |
| `plugin.py` | Config models, migration, lifecycle, hooks calling `clock` |
| `_manifest.json` | Static metadata |
| `config.default.toml` | Shipped defaults |
| `README.md` | Install / config / testing |
| `.gitignore` | `config.toml`, `config.local.toml`, `__pycache__`, `.venv` |
| `tests/test_formatter.py` | Format / resolve / dedupe / DST |
| `tests/test_inject.py` | Replace / append / on_no_match / historic parse |
| `tests/smoke_test.py` | Import, manifest, default config, end-to-end inject |

---

### Task 1: Pure clock core (`clock.py`)

**Files:**
- Create: `clock.py`
- Test: `tests/test_formatter.py`

**Interfaces:**
- Produces:
  - `normalize_zone_key(name: str) -> str` — `UTC`/`Etc/UTC` → `Etc/UTC` for ZoneInfo load; other names stripped as-is
  - `display_zone_name(name: str) -> str` — render `UTC` for UTC/Etc/UTC; else original IANA
  - `load_zone(name: str) -> ZoneInfo` — raises `ValueError` naming the bad id
  - `resolve_primary_timezone(override: str | None, *, environ: Mapping[str, str] | None = None, localtime_path: Path | None = None) -> str` — config → `TZ` → `/etc/localtime` symlink → else `ValueError`
  - `dedupe_world_timezones(primary: str, world: Sequence[str]) -> list[str]` — preserve order; drop entries whose normalize key equals primary’s
  - `format_clock_block(instant: datetime, primary: str, world: Sequence[str]) -> str` — 本地时间 + optional 世界时间
  - `parse_planner_time_message(text: str) -> datetime | None` — naive wall if whole text matches `时间：…`
  - `parse_replyer_time_prefix(text: str) -> tuple[datetime, str] | None` — `(naive_wall, remainder_including_leading_separators)` if starts with `当前时间：…`
  - `wall_to_instant(wall: datetime, primary: str) -> datetime` — attach primary tz (fold=0)
  - `format_line(instant: datetime, zone_name: str) -> str` — `- {display} ({abbr}): %Y-%m-%d %H:%M:%S`

- [ ] **Step 1: Write failing formatter tests**

```python
# tests/test_formatter.py
from __future__ import annotations
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))

from clock import (
    dedupe_world_timezones,
    display_zone_name,
    format_clock_block,
    load_zone,
    normalize_zone_key,
    resolve_primary_timezone,
    wall_to_instant,
)


def test_normalize_utc():
    assert normalize_zone_key("UTC") == "Etc/UTC"
    assert normalize_zone_key("Etc/UTC") == "Etc/UTC"
    assert display_zone_name("UTC") == "UTC"
    assert display_zone_name("Etc/UTC") == "UTC"


def test_dedupe_primary_out_of_world():
    assert dedupe_world_timezones("Asia/Shanghai", ["Asia/Shanghai", "UTC"]) == ["UTC"]
    assert dedupe_world_timezones("UTC", ["Asia/Shanghai", "Etc/UTC"]) == ["Asia/Shanghai"]


def test_format_block_with_world():
    instant = wall_to_instant(datetime(2026, 7, 28, 23, 24, 0), "America/Los_Angeles")
    text = format_clock_block(instant, "America/Los_Angeles", ["Asia/Shanghai", "UTC"])
    assert text.startswith("本地时间：\n- America/Los_Angeles (PDT): 2026-07-28 23:24:00")
    assert "世界时间：\n- Asia/Shanghai (CST): 2026-07-29 14:24:00\n- UTC (UTC): 2026-07-29 06:24:00" in text


def test_format_omits_world_when_empty_after_dedupe():
    instant = wall_to_instant(datetime(2026, 7, 29, 14, 24, 0), "Asia/Shanghai")
    text = format_clock_block(instant, "Asia/Shanghai", ["Asia/Shanghai"])
    assert text == "本地时间：\n- Asia/Shanghai (CST): 2026-07-29 14:24:00"
    assert "世界时间" not in text


def test_dst_abbreviation_changes():
    winter = wall_to_instant(datetime(2026, 1, 15, 12, 0, 0), "America/Los_Angeles")
    summer = wall_to_instant(datetime(2026, 7, 15, 12, 0, 0), "America/Los_Angeles")
    assert "(PST)" in format_clock_block(winter, "America/Los_Angeles", [])
    assert "(PDT)" in format_clock_block(summer, "America/Los_Angeles", [])


def test_load_zone_rejects_bad():
    try:
        load_zone("Not/AZone")
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "Not/AZone" in str(exc)


def test_resolve_primary_from_override(tmp_path, monkeypatch):
    assert resolve_primary_timezone("Europe/Berlin") == "Europe/Berlin"
```

- [ ] **Step 2: Run tests — expect import failure**

```bash
cd /mnt/klein/work/maibot-plugins/maibot-world-clock
PYTHONPATH=../maibot-plugin-sdk python -m pytest tests/test_formatter.py -v
```

Expected: FAIL (module `clock` missing)

- [ ] **Step 3: Implement `clock.py` core** (format/resolve/parse helpers only; inject in Task 2)

Implement the interfaces above. For `/etc/localtime`: if symlink, take basename path relative to a `zoneinfo` directory segment (e.g. `.../zoneinfo/America/Los_Angeles` → `America/Los_Angeles`). `resolve_primary_timezone` accepts optional `environ` and `localtime_path` for tests.

- [ ] **Step 4: Re-run formatter tests — expect PASS**

- [ ] **Step 5: Commit**

```bash
git add clock.py tests/test_formatter.py
git commit -m "feat(world-clock): add timezone resolve and format helpers"
```

---

### Task 2: Message inject helpers

**Files:**
- Modify: `clock.py`
- Test: `tests/test_inject.py`

**Interfaces:**
- Consumes: Task 1 formatters
- Produces:
  - `apply_planner_messages(messages: list[Any], *, primary: str, world: Sequence[str], mode: str, on_no_match: str, now: datetime | None = None) -> tuple[list[Any], str | None]`
    - Returns `(new_messages, warning_or_none)`. If `on_no_match=="error"` and replace finds nothing → raise `RuntimeError`.
  - `apply_replyer_messages(...)` — same signature/semantics for replyer patterns
  - Modes: `replace` | `append`; on_no_match: `warn_and_append` | `warn_only` | `error`
  - For multimodal content lists: rewrite first text part if it matches; else skip that message

- [ ] **Step 1: Write failing inject tests**

```python
# tests/test_inject.py — key cases
from datetime import datetime
from zoneinfo import ZoneInfo
from clock import apply_planner_messages, apply_replyer_messages

PRIMARY = "America/Los_Angeles"
WORLD = ["Asia/Shanghai", "UTC"]


def test_planner_replace_preserves_historic_instant():
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "时间：2026-07-20 10:00:00"},
        {"role": "user", "content": "时间：2026-07-28 23:24:00"},
    ]
    frozen_now = datetime(2099, 1, 1, 0, 0, 0, tzinfo=ZoneInfo(PRIMARY))
    out, warn = apply_planner_messages(
        messages, primary=PRIMARY, world=WORLD, mode="replace",
        on_no_match="warn_only", now=frozen_now,
    )
    assert warn is None
    assert "2026-07-20 10:00:00" in out[1]["content"]
    assert "2099" not in out[1]["content"]
    assert "2026-07-28 23:24:00" in out[2]["content"]
    assert out[1]["content"].startswith("本地时间：")


def test_planner_append_leaves_host_and_adds_now():
    messages = [{"role": "user", "content": "时间：2026-07-28 23:24:00"}]
    frozen_now = datetime(2026, 7, 28, 23, 30, 0, tzinfo=ZoneInfo(PRIMARY))
    out, _ = apply_planner_messages(
        messages, primary=PRIMARY, world=WORLD, mode="append",
        on_no_match="warn_only", now=frozen_now,
    )
    assert out[0]["content"] == "时间：2026-07-28 23:24:00"
    assert out[-1]["content"].startswith("本地时间：")
    assert "23:30:00" in out[-1]["content"]


def test_planner_replace_no_match_warn_and_append():
    messages = [{"role": "user", "content": "hello"}]
    frozen_now = datetime(2026, 7, 28, 23, 30, 0, tzinfo=ZoneInfo(PRIMARY))
    out, warn = apply_planner_messages(
        messages, primary=PRIMARY, world=WORLD, mode="replace",
        on_no_match="warn_and_append", now=frozen_now,
    )
    assert warn is not None
    assert out[-1]["content"].startswith("本地时间：")


def test_replyer_replace_keeps_trailing_sections():
    content = "当前时间：2026-07-28 23:24:00\n\n目标消息：你好"
    messages = [{"role": "user", "content": content}]
    out, _ = apply_replyer_messages(
        messages, primary=PRIMARY, world=WORLD, mode="replace",
        on_no_match="warn_only", now=None,
    )
    assert out[0]["content"].startswith("本地时间：")
    assert out[0]["content"].endswith("目标消息：你好")
    assert "当前时间：" not in out[0]["content"]
```

- [ ] **Step 2: Run — expect FAIL (functions missing)**

- [ ] **Step 3: Implement inject helpers in `clock.py`**

- [ ] **Step 4: Run — expect PASS**

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(world-clock): add planner/replyer message inject helpers"
```

---

### Task 3: Plugin wiring (`plugin.py` + packaging)

**Files:**
- Create: `plugin.py`, `_manifest.json`, `config.default.toml`, `.gitignore`, `README.md`
- Test: extend `tests/smoke_test.py`

**Interfaces:**
- `CURRENT_CONFIG_VERSION = "1.0.0"`
- Config sections `plugin` + `clock` matching the spec
- `WorldClockPlugin` with `config_model`, `on_load` / `on_unload` / `on_config_update`, two `@HookHandler`s
- `create_plugin() -> WorldClockPlugin`
- Hooks call `apply_*_messages` when `enabled`; log warnings from `on_no_match`; return `modified_kwargs`

- [ ] **Step 1: Write smoke test skeleton** (import plugin, read manifest, validate default config, run one inject through plugin settings builder)

- [ ] **Step 2: Implement packaging + plugin.py**

Config fields (WebUI labels in zh-CN):

```python
class ClockSectionConfig(PluginConfigBase):
    primary_timezone: str | None = Field(default=None, description="本地时区覆盖（IANA）；留空则自动解析进程时区")
    world_timezones: list[str] = Field(default_factory=lambda: ["Asia/Shanghai", "UTC"], description="世界时区列表（IANA）")
    mode: str = Field(default="replace", description="注入模式：replace / append")
    on_no_match: str = Field(default="warn_and_append", description="replace 未匹配时的行为")
```

Validate IANA on normalize/load path in `_normalize_world_clock_config` / settings build — call `load_zone` for primary override and each world entry.

Hook outline:

```python
@HookHandler("maisaka.planner.before_request", name="world_clock_planner",
             description="将规划器时间提示改写为带时区的本地/世界时间",
             mode=HookMode.BLOCKING, order=HookOrder.NORMAL)
async def on_planner_before_request(self, **kwargs):
    ...
```

Same for `maisaka.replyer.before_model_request`.

- [ ] **Step 3: Run full test suite**

```bash
PYTHONPATH=../maibot-plugin-sdk python -m pytest tests/ -v
PYTHONPATH=../maibot-plugin-sdk python tests/smoke_test.py
```

Expected: all PASS

- [ ] **Step 4: Commit**

```bash
git commit -m "feat(world-clock): wire hooks, config, manifest, and README"
```

---

### Task 4: Final verification

- [ ] **Step 1:** Confirm formatter + inject + smoke all pass
- [ ] **Step 2:** Confirm no Host/SDK files modified
- [ ] **Step 3:** Commit any leftover docs/plan updates

```bash
git add docs/superpowers/plans/2026-07-28-maibot-world-clock.md
git commit -m "docs(world-clock): add implementation plan"
```

---

## Spec coverage checklist

| Spec requirement | Task |
|---|---|
| Format 本地/世界 + dedupe | 1 |
| Parse historic wall time | 1–2 |
| Planner replace/append | 2–3 |
| Replyer replace/append | 2–3 |
| primary override + auto resolve | 1, 3 |
| Default Shanghai+UTC | 3 |
| mode + on_no_match | 2–3 |
| Manifest / README / config.default | 3 |
| Offline tests | 1–4 |
