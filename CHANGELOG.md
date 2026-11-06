# Changelog

本插件遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号与 metadata.yaml 保持一致。

## [v0.1.0] - 2026-11-06

### 新增

- LLM 工具 `selfmanage_update_persona_prompt`：读取工作区文件，更新当前会话
  人格的 system_prompt（写入人格数据库记录，含正常/异常回执与防止误清空其它字段）。
- LLM 工具 `selfmanage_get_recent_logs`：读取最近 N 分钟（默认 5 分钟）的
  AstrBot 日志，支持 debug/info/warn/error 过滤、行数上限与截断说明；
  内存缓冲（loguru sink）为主，`data/logs` 文件兜底。
- 聊天指令 `/selfmanage_logs`（管理员）、`/selfmanage_persona`（只读人格）。
- 配置项：`require_admin`、`log_buffer_size`、`log_default_minutes`、
  `log_max_return_lines`、`log_file_fallback`、`persona_prompt_max_kb`。
