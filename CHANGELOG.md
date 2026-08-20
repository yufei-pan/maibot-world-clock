# Changelog

本文件记录 maibot-world-clock（世界时钟）的版本变更。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/)，
版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [0.1.1] - 2026-08-19

### 修复

- 兼容 MaiBot 1.2.0 的 Item-first planner / replyer Hook 载荷，同时保留旧版 `messages` 载荷支持

## [0.1.0] - 2026-07-29

### 新增

- 首次发布：把 Host 注入的无时区时间提示改写（或追加）为带 IANA 与缩写的本地 / 世界时间块
