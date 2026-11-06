"""近期日志收集与查询。

设计（对照 AstrBot 4.28/4.29 源码核实）：
- AstrBot 把标准 logging 输出全部桥接到 loguru（core/log.py 的
  ``_LoguruInterceptHandler`` 挂在各自 logger 上，且 ``GetLogger`` 会设
  ``propagate=False``），因此直接往 loguru 挂 sink 是覆盖面最准的采集方式；
  loguru 不可用（导入失败等）时降级为挂标准 logging root handler。
- 采集进内存环形缓冲（覆盖从挂载时刻起的记录），结构化存储，不做文本解析。
- 缓冲覆盖不到的历史时间窗用 ``data/logs/*.log*`` 文件兜底，宽容正则解析
  时间戳/级别，无时间戳行是 traceback 等续行，并入上一条并继承其级别。
  （默认日志文件为 data/logs/astrbot.log，见 LogManager._default_log_path。）
"""

from __future__ import annotations

import datetime as _dt
import logging
import re
import time
import traceback
from collections import deque
from dataclasses import dataclass
from pathlib import Path

_LOG_TIME_FORMAT = "%Y-%m-%d %H:%M:%S.%f"

_LEVEL_ORDER = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

_LEVEL_ALIAS = {
    "debug": "DEBUG",
    "info": "INFO",
    "warn": "WARNING",
    "warning": "WARNING",
    "err": "ERROR",
    "error": "ERROR",
    "fatal": "CRITICAL",
    "critical": "CRITICAL",
}

# 单次返回内容的硬上限，避免撑爆模型上下文
_MAX_OUTPUT_CHARS = 60000

# 文件兜底：形如 2026-11-06 23:41:02(.123) 的时间戳（允许 T 分隔与毫秒）
_FILE_TS_RE = re.compile(r"20\d{2}-\d{2}-\d{2}[ T]\d{1,2}:\d{2}:\d{2}(?:[.,]\d{1,3})?")
# 文件兜底：级别词（限有边界匹配，避免误吞普通单词）
_FILE_LEVEL_RE = re.compile(r"\b(DEBUG|INFO|WARNING|ERROR|CRITICAL|WARN|FATAL)\b")


@dataclass(frozen=True)
class LogEntry:
    """一条结构化日志记录。message 允许多行（traceback/续行都已并入）。"""

    ts: float
    level: str
    name: str
    message: str


def normalize_levels(raw: str | None) -> set[str] | None:
    """把逗号分隔的级别字符串归一化为大写级别集合。

    支持 debug/info/warn(+warning)/error/fatal(+critical) 与别名缩写。
    返回 ``None`` 表示不过滤（全部级别）。
    """
    if raw is None:
        return None
    raw = raw.strip()
    if not raw:
        return None
    levels: set[str] = set()
    for token in re.split(r"[,，;；\s]+", raw):
        token = token.strip().lower()
        if not token:
            continue
        if token in _LEVEL_ALIAS:
            levels.add(_LEVEL_ALIAS[token])
        elif token.upper() in _LEVEL_ORDER:
            levels.add(token.upper())
        else:
            raise ValueError(f"不支持的日志级别: {token!r}")
    return levels or None


def format_entries(entries: list[LogEntry]) -> str:
    """把 LogEntry 格式化为文本：每条首行 `[时间] [级别] [来源] 消息`。"""
    lines: list[str] = []
    for entry in entries:
        ts_str = _dt.datetime.fromtimestamp(entry.ts).strftime(_LOG_TIME_FORMAT)[:-3]
        message_lines = entry.message.splitlines() or [entry.message]
        lines.append(f"[{ts_str}] [{entry.level}] [{entry.name}] {message_lines[0]}")
        lines.extend(f"    {continuation}" for continuation in message_lines[1:])
    return "\n".join(lines)


def _clip_message(message: str, max_lines: int) -> str:
    """单条消息超出预算时按行裁剪（保留开头部分，截断说明放尾行）。"""
    lines = message.splitlines() or [message]
    clipped = lines[: max(1, max_lines)]
    text = "\n".join(clipped)
    if len(clipped) < len(lines):
        text = text + "\n    ...（该条消息过长，已截断）"
    return text


def _render_with_budget(entries: list[LogEntry], max_lines: int) -> tuple[str, int]:
    """在行数与字符预算内渲染，返回 (文本, 溢出条数)。

    entries 需按时间正序。从最新往旧收集，超预算即停，输出时恢复时间正序
    （丢弃最早溢出的部分）。若预算被单条巨量条目挤爆且尚未保留任何条目，
    则保底裁剪保留这一条，避免出现“只报溢出、不给内容”。
    """
    kept: list[LogEntry] = []
    used_lines = 0
    overflow = 0
    for entry in reversed(entries):
        cost = entry.message.count("\n") + 1
        if used_lines + cost > max_lines:
            if not kept:
                clipped = LogEntry(
                    entry.ts,
                    entry.level,
                    entry.name,
                    _clip_message(entry.message, max_lines),
                )
                kept.append(clipped)
                overflow = len(entries) - len(kept)
                break
            overflow = len(entries) - len(kept)
            break
        kept.append(entry)
        used_lines += cost
    kept.reverse()
    text = format_entries(kept)
    if len(text) > _MAX_OUTPUT_CHARS:
        text = text[-_MAX_OUTPUT_CHARS:]
    return text, overflow


def filter_entries(
    entries: list[LogEntry],
    cutoff_ts: float,
    levels: set[str] | None = None,
) -> list[LogEntry]:
    """按时间下限与级别集合过滤（levels 为 None 表示不过滤）。"""
    return [
        entry
        for entry in entries
        if entry.ts >= cutoff_ts and (levels is None or entry.level in levels)
    ]


def collect_from_buffer(
    buffer: deque[LogEntry],
    cutoff_ts: float,
    levels: set[str] | None,
    max_lines: int,
) -> str | None:
    """查内存环形缓冲，返回格式化文本；未命中返回 None。"""
    matched = filter_entries(list(buffer), cutoff_ts, levels)
    if not matched:
        return None
    text, overflow = _render_with_budget(matched, max_lines)
    if overflow:
        text = (
            f"（时间窗内共 {len(matched)} 条，超出 {max_lines} 行上限，仅展示最新的部分）\n"
            + text
        )
    return text


def scan_log_files(
    logs_dir: str | Path,
    cutoff_ts: float,
    levels: set[str] | None,
    max_lines: int,
) -> str | None:
    """扫描 ``data/logs`` 下所有 ``*.log*`` 文件，作为时间窗兜底。

    只纳入时间戳不低于 cutoff 的行；无时间戳的续行并入最近一条已接受条目。
    返回格式化文本（可能带截断说明），未命中返回 None。
    """
    dir_path = Path(logs_dir)
    if not dir_path.is_dir():
        return None
    files = sorted(
        (p for p in dir_path.glob("*.log*") if p.is_file()),
        key=lambda p: p.name,
    )
    if not files:
        return None

    accepted: list[LogEntry] = []
    last: LogEntry | None = None  # 最近一条“已接受”的条目，续行并入它
    for file_path in files:
        try:
            with file_path.open("r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.rstrip("\n").rstrip("\r").rstrip()
                    if not line:
                        continue
                    ts_match = _FILE_TS_RE.search(line)
                    if ts_match is None:
                        if last is not None:
                            last = LogEntry(
                                last.ts,
                                last.level,
                                last.name,
                                last.message + "\n" + line,
                            )
                            accepted[-1] = last
                        continue
                    try:
                        ts = _parse_ts(ts_match.group(0))
                    except ValueError:
                        continue
                    if ts < cutoff_ts:
                        last = None  # 窗口外：其续行也不属于本查询
                        continue
                    level_match = _FILE_LEVEL_RE.search(line, ts_match.end())
                    raw_level = level_match.group(0) if level_match else ""
                    level = _LEVEL_ALIAS.get(
                        raw_level.lower(), raw_level.upper() or "INFO"
                    )
                    name = _extract_logger_name(line, level_match)
                    if level_match:
                        message = line[level_match.end() :].lstrip("]:： ")
                    else:
                        message = line[ts_match.end() :].lstrip()
                    entry = LogEntry(ts, level, name, message or line)
                    last = None
                    if levels is None or level in levels:
                        accepted.append(entry)
                        last = entry
        except OSError:
            continue

    if not accepted:
        return None
    text, overflow = _render_with_budget(accepted, max_lines)
    if overflow > 0:
        text = (
            f"（文件兜底：时间窗内超出上限 {max_lines} 行，另有 {overflow} 条未展示）\n"
            + text.split("\n", 1)[-1]
        )
    return text


def _parse_ts(raw: str) -> float:
    raw = raw.replace("T", " ").replace(",", ".")
    fmt = "%Y-%m-%d %H:%M:%S.%f" if "." in raw else "%Y-%m-%d %H:%M:%S"
    return _dt.datetime.strptime(raw, fmt).timestamp()


def _extract_logger_name(line: str, level_match: re.Match | None) -> str:
    """从日志行尽量取 logger/模块名；宽容策略，取不到为空即可。"""
    if level_match is None:
        return ""
    rest = line[level_match.end() :]
    module_match = re.match(r"\s*[\w.]+(?=[\(:>])", rest)
    return module_match.group(0) if module_match else ""


def _loguru_record_message(record) -> str:
    text = str(record["message"])
    exception = record.get("exception")
    if exception:
        try:
            text = (
                text
                + "\n"
                + "".join(traceback.format_exception(*exception, limit=20)).rstrip()
            )
        except Exception:  # noqa: BLE001, S110 - 附加失败不影响主消息，静默即可
            pass
    return text


class _RootHandlerBridge(logging.Handler):
    """loguru 不可用时的兜底：抓沿标准 logging root 冒泡的记录。"""

    def __init__(self, sink) -> None:
        super().__init__(level=logging.DEBUG)
        self._sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        self._sink(record)

    def flush(self) -> None:
        return


class RecentLogBuffer:
    """近期日志环形缓冲 + 采集器挂载（模块级单例 ``current``）。"""

    def __init__(self) -> None:
        self._buffer: deque[LogEntry] = deque(maxlen=1)
        self._loguru_sink_id: int | None = None
        self._root_handler: _RootHandlerBridge | None = None
        self._start_ts: float = 0.0
        self._mode: str = "detached"

    # 查询面采用属性代理，避免外部直接改内部 deque。
    @property
    def entries(self) -> list[LogEntry]:
        return list(self._buffer)

    @property
    def start_ts(self) -> float:
        """缓冲可回溯的最早时刻（挂载时刻；未挂载为 0）。"""
        return self._start_ts

    @property
    def mode(self) -> str:
        """当前采集方式：loguru / logging-root / detached。"""
        return self._mode

    def attach(self, buffer_size: int, prefer: str | None = None) -> str:
        """挂载采集器，返回实际采用的采集方式（loguru / logging-root）。

        ``prefer`` 仅供自测/故障切换：``"logging-root"`` 时跳过 loguru，
        强制走标准 logging root handler。
        """
        self.detach()
        size = max(100, int(buffer_size))
        self._buffer = deque(maxlen=size)
        self._start_ts = time.time()

        if prefer != "logging-root":
            sink = self._loguru_sink
            try:
                from loguru import logger as loguru_logger

                self._loguru_sink_id = loguru_logger.add(
                    sink, level=0, backtrace=False, diagnose=False, catch=True
                )
                self._mode = "loguru"
                return self._mode
            except Exception:  # noqa: BLE001 - loguru 缺失/挂载失败 → 兜底
                self._loguru_sink_id = None

        self._root_handler = _RootHandlerBridge(self._on_record)
        logging.getLogger().addHandler(self._root_handler)
        self._mode = "logging-root"
        return self._mode

    def detach(self) -> None:
        """卸载采集器。插件 stop/重载时必须调用，避免重复挂载。"""
        if self._loguru_sink_id is not None:
            try:
                from loguru import logger as loguru_logger

                loguru_logger.remove(self._loguru_sink_id)
            except Exception:  # noqa: BLE001, S110 - sink 已不在时忽略
                pass
            self._loguru_sink_id = None
        if self._root_handler is not None:
            logging.getLogger().removeHandler(self._root_handler)
            self._root_handler.close()
            self._root_handler = None
        self._buffer = deque(maxlen=1)
        self._mode = "detached"

    # ---- 捕获 ----

    def _on_loguru(self, message) -> None:
        record = message.record
        level = _LEVEL_ALIAS.get(
            record["level"].name.lower(), record["level"].name.upper()
        )
        self._buffer.append(
            LogEntry(
                ts=record["time"].timestamp(),
                level=level,
                name=str(record["name"] or ""),
                message=_loguru_record_message(record),
            )
        )

    # sink 会被 loguru 保存，用稳定的私有方法引用而非 lambda。
    _loguru_sink = _on_loguru

    def _on_record(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if record.exc_info and record.exc_info[1] is not None:
            message = (
                message
                + "\n"
                + "".join(
                    traceback.format_exception(*record.exc_info, limit=20)
                ).rstrip()
            )
        self._buffer.append(
            LogEntry(
                ts=record.created,
                level=_LEVEL_ALIAS.get(
                    record.levelname.lower(), record.levelname.upper()
                ),
                name=record.name,
                message=message,
            )
        )


# 模块级单例：采集器与宿主全局日志体系交互，全进程共享一份。
current = RecentLogBuffer()
