# 世界时钟（World Clock）

MaiBot 插件：在 Maisaka **规划器**与**回复器**向 LLM 发请求前，把 Host 注入的无时区时间提示改写为带 IANA 与缩写的本地 / 世界时间块。

## 效果示例

Host 原文：

```text
时间：2026-07-28 23:24:00
```

插件改写后（本地为 `America/Los_Angeles` 时）：

```text
本地时间：
- America/Los_Angeles (PDT): 2026-07-28 23:24:00
世界时间：
- Asia/Shanghai (CST): 2026-07-29 14:24:00
- UTC (UTC): 2026-07-29 06:24:00
```

改写时会**读取原消息里的墙钟时间**（含跨日边界的历史时间戳），不会用“现在”覆盖历史时间。

## 安装

1. 将本仓库放到 `MaiBot/plugins/maibot-world-clock/`（或从工作区 sibling 目录符号链接进去）。
2. 重启 Host，或在 WebUI 加载插件。
3. 首次加载会从 `config.default.toml` 生成运行期 `config.toml`。

```bash
ln -s ../../maibot-world-clock MaiBot/plugins/maibot-world-clock
```

## 配置

见 `config.default.toml`：

| 字段 | 默认 | 说明 |
|---|---|---|
| `plugin.enabled` | `true` | 总开关 |
| `clock.primary_timezone` | （空） | 本地时区覆盖；空则用 `TZ` 或 `/etc/localtime` |
| `clock.world_timezones` | `Asia/Shanghai`, `UTC` | 世界时区列表；与本地相同的项会去掉 |
| `clock.mode` | `replace` | `replace` 改写 Host 字符串；`append` 只追加 |
| `clock.on_no_match` | `warn_and_append` | `replace` 未匹配时：警告并追加 / 仅警告 / 抛错 |

无效 IANA 名会在配置校验阶段直接失败，不会静默跳过。

若 Host 将来改了时间文案导致 `replace` 匹配失败，可把 `mode` 改为 `append` 继续使用。

## Hook

- `maisaka.planner.before_request` — 匹配整段 `时间：YYYY-MM-DD HH:MM:SS`
- `maisaka.replyer.before_model_request` — 匹配开头 `当前时间：YYYY-MM-DD HH:MM:SS`

## 测试

```bash
cd maibot-world-clock
PYTHONPATH=../maibot-plugin-sdk python -m pytest tests/ -v
PYTHONPATH=../maibot-plugin-sdk python tests/smoke_test.py
```

## 许可

MIT
