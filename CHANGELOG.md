# Changelog

本文件记录 maibot-world-clock（世界时钟）的版本变更。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/)，
版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### 变更

- manifest 兼容上限改为实际验证过的范围：宿主 `max_version` 1.99.99 → **1.3.99**，SDK `max_version` 2.99.99 → **2.14.99**（宿主 1.3.8 + SDK 2.12.0 上实际运行，SDK 2.14.0 上测试通过）。插件仓库审核不再接受「永远兼容」式的上限

## [0.1.1] - 2026-08-19

### 修复

- 兼容 MaiBot 1.2.0 的 Item-first planner / replyer Hook 载荷，同时保留旧版 `messages` 载荷支持

## [0.1.0] - 2026-07-29

### 新增

- 首次发布：把 Host 注入的无时区时间提示改写（或追加）为带 IANA 与缩写的本地 / 世界时间块
