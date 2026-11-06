"""astrbot_plugin_self_manage —— 让 Agent 自我维护 AstrBot。

注册两个 LLM 工具 + 两个普通指令：
1. ``selfmanage_update_persona_prompt``：读工作区文件内容，替换当前会话
   人格的 system_prompt（写入人格数据库记录）。
2. ``selfmanage_get_recent_logs``：拿最近 N 分钟的 AstrBot 日志，支持按
   debug/info/warn/error 过滤。
3. ``/selfmanage_logs`` ``/selfmanage_persona``：人工验证入口。

业务逻辑都在 core/（本文件只做注册与转发），便于无宿主自测；API 语义
出处见 core/ 各模块文件头注释。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import (
    get_astrbot_data_path,
    get_astrbot_root,
)

from .core import log_collector, persona_ops

_preview_chars = 60
_prompt_show_chars = 500

_MAX_LOG_MINUTES = 1440  # 单次回看上限：1 天


class SelfManagePlugin(Star):
    """Agent 自我维护插件：更新人格提示词 / 读取近期日志。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        buffer_size = int(self.config.get("log_buffer_size", 4000))
        mode = log_collector.current.attach(buffer_size)
        logger.info(f"[self_manage] 日志采集已挂载：mode={mode}, buffer={buffer_size}")

    async def terminate(self) -> None:
        log_collector.current.detach()

    # ------------------------------------------------------------------
    # 配置读取（默认值与 _conf_schema.json 保持同源语义）
    # ------------------------------------------------------------------

    def _cfg(self, key: str, default):
        return self.config.get(key, default)

    def _gate_admin(self, event: AstrMessageEvent) -> str | None:
        """工具的管理员门槛。返回拦截文案，未拦截返回 None。"""
        if bool(self._cfg("require_admin", False)) and not event.is_admin():
            return "需要管理员权限才能使用该工具。"
        return None

    # ------------------------------------------------------------------
    # 工具 1：更新人格提示词
    # ------------------------------------------------------------------

    @filter.llm_tool(name="selfmanage_update_persona_prompt")
    async def llm_update_persona_prompt(
        self, event: AstrMessageEvent, file_path: str
    ) -> str:
        """[Self Manage] 用文件内容更新当前会话人格的 system_prompt。

        读取 Bot 主机上的一个文本文件，把文件全文作为提示词写入 AstrBot
        人格数据库记录（当前会话绑定的人格）。这是持久修改：下一次 LLM
        请求起生效；不影响开局对话/工具列表等其它人格字段。文件必须真实
        存在于 Bot 主机，相对路径按 AstrBot 工作目录解析。

        Args:
            file_path(string): 提示词文件在 Bot 主机上的完整路径
        """
        denied = self._gate_admin(event)
        if denied:
            return denied
        umo = event.unified_msg_origin
        try:
            max_bytes = int(self._cfg("persona_prompt_max_kb", 100)) * 1024
            content = await asyncio.to_thread(
                persona_ops.read_prompt_file,
                str(file_path or ""),
                get_astrbot_root(),
                max_bytes,
            )
            persona_id, note = await persona_ops.resolve_session_persona_id(
                self.context, umo, event.get_platform_name() or ""
            )
            if not persona_id:
                return f"未能确定要更新的人格：{note}"
            persona_mgr = self.context.persona_manager
            old_prompt = await persona_ops.current_persona_prompt(
                persona_mgr, persona_id
            )
            if content == old_prompt:
                return (
                    f"人格 [{persona_id}] 的提示词与文件内容一致，未做修改"
                    f"（当前共 {len(content)} 字符）。"
                )
            await persona_ops.update_persona_prompt(persona_mgr, persona_id, content)
        except persona_ops.PromptFileError as e:
            return f"更新人格失败：{e}"
        except ValueError:
            return (
                f"人格记录不存在：[{persona_id}]。请在 WebUI 人格页面确认该"
                "人格仍存在，或为会话重新绑定人格。"
            )
        except Exception as e:  # noqa: BLE001 - 工具消息兜底，不让宿主层裸抛
            logger.error(f"[self_manage] 更新人格异常：{e!r}", exc_info=True)
            return f"更新人格时出错：{e}"

        logger.info(
            f"[self_manage] 人格 [{persona_id}] 提示词已更新："
            f"{len(old_prompt)} -> {len(content)} 字符，来源文件 {file_path}"
        )
        return (
            f"已更新人格 [{persona_id}] 的提示词：{len(old_prompt)} -> "
            f"{len(content)} 字符。新提示词开头：{content[:_preview_chars]!r}"
        )

    # ------------------------------------------------------------------
    # 工具 2：读取近期日志
    # ------------------------------------------------------------------

    @filter.llm_tool(name="selfmanage_get_recent_logs")
    async def selfmanage_get_recent_logs(
        self,
        event: AstrMessageEvent,
        minutes: str = "",
        levels: str = "",
    ) -> str:
        """[Self Manage] 获取最近若干分钟的 AstrBot 日志。

        返回当前 Bot 进程的日志文本（含 traceback 续行）。默认回看 5 分钟，
        可按级别过滤（debug/info/warn/error，warn 是 warning 的别名）。日志
        内存缓冲从插件挂载时开始记录，更早的时间窗会自动回读 data/logs
        下的日志文件。

        Args:
            minutes(number): 回看分钟数，默认 5
            levels(string): 逗号分隔的级别名如 error,warn；留空返回全部
        """
        denied = self._gate_admin(event)
        if denied:
            return denied
        default_minutes = int(self._cfg("log_default_minutes", 5))
        minutes_int = _coerce_minutes(minutes, default_minutes)
        try:
            level_set = log_collector.normalize_levels(levels)
        except ValueError:
            return (
                f"不认识的级别：{levels!r}。可用级别：debug / info / warn "
                "(=warning) / error，多个用英文逗号分隔，留空表示全部。"
            )
        return await self._query_logs(minutes_int, level_set)

    async def _query_logs(self, minutes: int, level_set: set[str] | None) -> str:
        buffer = log_collector.current
        cutoff = time.time() - minutes * 60
        max_lines = int(self._cfg("log_max_return_lines", 300))
        levels_desc = ",".join(sorted(level_set)) if level_set else "全部"
        use_buffer = buffer.mode != "detached"

        text: str | None = None
        source = ""
        if use_buffer and buffer.start_ts <= cutoff:
            text = log_collector.collect_from_buffer(
                buffer.entries, cutoff, level_set, max_lines
            )
            source = f"内存缓冲[{buffer.mode}]"
        if text is None and bool(self._cfg("log_file_fallback", True)):
            logs_dir = Path(get_astrbot_data_path()) / "logs"
            # 文件扫描是同步 IO，放线程避免卡事件循环
            text = await asyncio.to_thread(
                log_collector.scan_log_files, logs_dir, cutoff, level_set, max_lines
            )
            source = "日志文件兜底 data/logs"
        if text is None and use_buffer:
            # 缓冲未覆盖该时间窗且文件兜底关闭/未命中：仍返回缓冲内的部分
            text = log_collector.collect_from_buffer(
                buffer.entries, cutoff, level_set, max_lines
            )
            source = f"内存缓冲[{buffer.mode}]（缓冲未完整覆盖该时间窗）"
        if not text:
            return (
                f"[self_manage] 最近 {minutes} 分钟（级别 {levels_desc}）"
                f"没有匹配的日志。查看范围：{source or '无'}。"
            )
        return (
            f"[self_manage] 最近 {minutes} 分钟日志"
            f"（级别 {levels_desc}，来源：{source}）\n{text}"
        )

    # ------------------------------------------------------------------
    # 指令：人工验证入口
    # ------------------------------------------------------------------

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("selfmanage_logs", alias={"自维护日志"})
    async def cmd_logs(
        self, event: AstrMessageEvent, minutes: int = 0, levels: str = ""
    ):
        """/selfmanage_logs [分钟数] [级别]：查看近期日志（管理员）。"""
        default_minutes = int(self._cfg("log_default_minutes", 5))
        minutes_int = minutes if minutes and minutes > 0 else default_minutes
        try:
            level_set = log_collector.normalize_levels(levels)
        except ValueError:
            yield event.plain_result(
                f"不认识的级别：{levels!r}。可用：debug/info/warn/error，"
                "用英文逗号分隔，留空表示全部。"
            )
            return
        yield event.plain_result(await self._query_logs(minutes_int, level_set))

    @filter.command("selfmanage_persona", alias={"自维护人格"})
    async def cmd_persona(self, event: AstrMessageEvent):
        """/selfmanage_persona：查看当前会话的人格 id 与提示词（只读）。"""
        persona_id, note = await persona_ops.resolve_session_persona_id(
            self.context, event.unified_msg_origin, event.get_platform_name() or ""
        )
        if not persona_id:
            yield event.plain_result(f"未能确定人格：{note}")
            return
        try:
            prompt = await persona_ops.current_persona_prompt(
                self.context.persona_manager, persona_id
            )
        except ValueError:
            yield event.plain_result(
                f"人格 [{persona_id}] 在人格数据库中不存在（会话绑定值可能已失效）。"
            )
            return
        shown = prompt[:_prompt_show_chars]
        tail = "…" if len(prompt) > len(shown) else ""
        yield event.plain_result(
            f"当前会话人格：[{persona_id}]（共 {len(prompt)} 字符）\n"
            f"提示词开头：\n{shown}{tail}"
        )


def _coerce_minutes(raw: str | float, default: int) -> int:
    """把工具传参归一为分钟数；非法/越界时回退默认，并限制上限 1 天。"""
    try:
        value = int(float(str(raw).strip())) if str(raw).strip() else default
    except (TypeError, ValueError):
        value = default
    return max(min(value, _MAX_LOG_MINUTES), 1)
