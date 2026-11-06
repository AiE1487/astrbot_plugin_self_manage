"""stub 宿主自测：无 AstrBot 环境验证 core/ 纯逻辑。

用法：python .v2c/selftest.py   （在插件目录下执行）
桩只提供 core/ 实际 import 的名字；main.py 依赖真实宿主，不在本测试范围。
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import sys
import time
from pathlib import Path

V2C = Path(__file__).resolve().parent
REPO = V2C.parent
SANDBOX = V2C / "sandbox"

sys.path.insert(0, str(V2C / "stub"))  # 先找到桩宿主
sys.path.insert(0, str(REPO))  # 再找到插件本体

shutil.rmtree(SANDBOX, ignore_errors=True)
SANDBOX.mkdir(parents=True, exist_ok=True)

from core import log_collector as lc
from core import persona_ops as po

PASS: list[str] = []


def _check(name: str, cond: bool, detail: str = "") -> None:
    if not cond:
        raise AssertionError(f"FAIL: {name} {detail}")
    PASS.append(name)


# ---------------------------------------------------------------- log_collector


def test_normalize_levels() -> None:
    _check(
        "levels warn,error", lc.normalize_levels("warn,error") == {"WARNING", "ERROR"}
    )
    _check("levels 大小写", lc.normalize_levels(" Info, debug ") == {"INFO", "DEBUG"})
    _check(
        "levels 中文逗号", lc.normalize_levels("error，warn") == {"ERROR", "WARNING"}
    )
    _check("levels 空串 None", lc.normalize_levels("   ") is None)
    _check("levels None", lc.normalize_levels(None) is None)
    try:
        lc.normalize_levels("trace")
        _check("levels 非法级别应抛错", False)
    except ValueError:
        _check("levels 非法级别应抛错", True)


def test_filter_and_format() -> None:
    now = 1000.0
    entries = [
        lc.LogEntry(now - 400, "INFO", "a.mod", "i-1"),
        lc.LogEntry(now + 1, "ERROR", "b.mod", "err\nTraceback line2"),
        lc.LogEntry(now + 2, "DEBUG", "c.mod", "d-1"),
        lc.LogEntry(now + 6, "WARNING", "d.mod", "w-1"),
    ]
    got = lc.filter_entries(entries, cutoff_ts=now, levels={"ERROR", "WARNING"})
    _check(
        "filter 只留窗口+级别",
        [e.message.split("\n")[0] for e in got] == ["err", "w-1"],
    )
    got_all = lc.filter_entries(entries, cutoff_ts=now - 350, levels=None)
    _check("filter 不限级别", len(got_all) == 3)
    text = lc.format_entries(got)
    _check(
        "format 首行样式",
        text.splitlines()[0].startswith("[") and "WARNING" in text.splitlines()[-1],
    )
    _check("format 续行缩进", "    Traceback line2" in text)


def test_collect_from_buffer_budget() -> None:
    now = 5000.0
    # 场景 A：巨量条目是最新一条，单独挤爆预算 → 保底裁剪保留 + 溢出说明
    buffer_a = [
        lc.LogEntry(now - 3, "ERROR", "x", "e2"),
        lc.LogEntry(now - 2, "ERROR", "x", "e1"),
        lc.LogEntry(now - 1, "INFO", "x", "\n".join(f"l{i}" for i in range(200))),
    ]
    text = lc.collect_from_buffer(
        buffer_a, cutoff_ts=now - 100, levels=None, max_lines=50
    )
    _check(
        "budget 保底裁剪",
        text is not None
        and text.startswith("（时间窗内")
        and "l0" in text
        and "e1" not in text,
        text[:80],
    )

    # 场景 B：多条小记录超预算 → 保留最新、丢最早
    many = [lc.LogEntry(now - (59 - i), "INFO", "x", f"m{i}") for i in range(60)]
    text2 = lc.collect_from_buffer(
        many, cutoff_ts=now - 2000, levels=None, max_lines=30
    )
    _check(
        "budget 只留最新",
        text2 is not None and text2.startswith("（时间窗内共 60 条"),
        (text2 or "")[:80],
    )
    _check("budget 最新内容保留", "m59" in (text2 or "") and "m0" not in (text2 or ""))

    # 未命中
    _check(
        "buffer 未命中 None",
        lc.collect_from_buffer([], cutoff_ts=0, levels=None, max_lines=50) is None,
    )


def test_capture_and_lifecycle() -> None:
    buf = lc.RecentLogBuffer()
    # 统一走 logging-root 路径：不依赖本机是否装有 loguru（真宿主里才有 loguru 桥）
    mode = buf.attach(500, prefer="logging-root")
    _check("attach 强制 root 模式", mode == "logging-root")

    logging.getLogger("capture_x").warning("hello once")
    captured = [e for e in buf.entries if e.name.endswith("capture_x")]
    _check("root 冒泡记录被采集", bool(captured), f"entries={buf.entries[:3]}")

    logging.getLogger("capture_exc").exception("wrapped")
    got = [e for e in buf.entries if e.name.endswith("capture_exc")]
    _check(
        "异常 traceback 被并入", bool(got)
    )  # 当前路径仅 handler；有 exc 时根记录自带

    # loguru sink 协议：伪装一个 loguru Message 验证映射（真宿主里这是主路径）
    import datetime as _dt

    class _Level:
        name = "WARNING"

    msg = type("M", (), {})()
    msg.record = {
        "time": _dt.datetime.now(),
        "level": _Level(),
        "name": "astro.core",
        "message": "loguru path",
        "exception": None,
    }
    before = len(buf.entries)
    buf._loguru_sink(msg)
    _check(
        "loguru sink 采集",
        len(buf.entries) == before + 1
        and buf.entries[-1].message == "loguru path"
        and buf.entries[-1].level == "WARNING",
    )

    buf.detach()
    _check("detach 清空", not buf.entries)
    logging.getLogger("capture_x").warning("after detach")
    _check(
        "detach 后不再采集",
        not [e for e in buf.entries if e.name.endswith("capture_x")],
    )
    buf.attach(500, prefer="logging-root")
    buf.detach()
    _check("重复 attach/detach 幂等", buf.mode == "detached")


def test_scan_log_files() -> None:
    logs_dir = SANDBOX / "data" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    (logs_dir / "astrbot.log").write_text(
        "2026-11-06 10:01:05.123 [+++] [INFO] [log.py:12]: boot\n"
        "2026-11-06 10:02:00 [WARN] [chat.x]: something odd\n"
        "  line2 continuation\n"
        "2026-11-06 10:03:30.500 [ERROR] [pipeline]: failed to send\n"
        "Traceback(...):\n"
        "  raise ValueError\n",
        encoding="utf-8",
    )
    (logs_dir / "astrbot.2026-11-05_23-59-59_999999.log").write_text(
        "2026-11-05 23:00:00 [INFO] [old]: too old\n"
        "2026-11-06 09:59:00 [ERROR] [old]: just before window\n",
        encoding="utf-8",
    )
    ts = lc._parse_ts("2026-11-06 10:01:00")
    text = lc.scan_log_files(logs_dir, ts, {"ERROR", "WARNING"}, max_lines=50)
    _check(
        "file 兜底级别过滤",
        text is not None and "something odd" in text and "failed to send" in text,
    )
    _check("file 兜底续行并入", "    Traceback(...):" in text)
    _check("file 兜底老日志剔除", "too old" not in text and "just before" not in text)
    _check("file 兜底 INFO 剔除", "boot" not in text)
    text_all = lc.scan_log_files(logs_dir, ts, None, 50)
    _check("file 兜底不过滤级别", "boot" in text_all)
    _check(
        "file 兜底目录缺失 None",
        lc.scan_log_files(SANDBOX / "nope", ts, None, 50) is None,
    )
    _check("file 兜底目录无文件 None", lc.scan_log_files(SANDBOX, ts, None, 50) is None)


# ---------------------------------------------------------------- persona_ops


class _Conv:
    def __init__(self, persona_id):
        self.persona_id = persona_id


class _ConvMgr:
    def __init__(self, cid, persona_id):
        self._cid, self._pid = cid, persona_id

    async def get_curr_conversation_id(self, umo):
        return self._cid

    async def get_conversation(self, umo, cid):
        return _Conv(self._pid)


class _Persona:
    def __init__(self, system_prompt):
        self.system_prompt = system_prompt
        self.tools = "t"
        self.begin_dialogs = ["d1"]


class _PersonaMgr:
    def __init__(self, personas=None, default_name=None):
        self._personas = personas or {}
        self._default_name = default_name
        self.calls: list[tuple] = []

    async def get_persona(self, pid):
        if pid not in self._personas:
            raise ValueError(f"Persona with ID {pid} does not exist.")
        return self._personas[pid]

    async def update_persona(self, persona_id, **kw):
        self.calls.append((persona_id, kw))
        self._personas[persona_id].system_prompt = kw["system_prompt"]
        return self._personas[persona_id]

    async def get_default_persona_v3(self, umo=None):
        return {"name": self._default_name} if self._default_name else None


class _PersonaMgrWithResolver(_PersonaMgr):
    async def resolve_selected_persona(
        self, *, umo, conversation_persona_id, platform_name
    ):
        return conversation_persona_id, None, None, False


class _Ctx:
    def __init__(self, conv_mgr, persona_mgr):
        self.conversation_manager = conv_mgr
        self.persona_manager = persona_mgr


def _run(coro):
    return asyncio.run(coro)


def test_resolve_persona_chain() -> None:
    ctx = _Ctx(
        _ConvMgr("cid1", "my_persona"), _PersonaMgr({"my_persona": _Persona("old")})
    )
    pid, note = _run(po.resolve_session_persona_id(ctx, "chat:1:u1", "webchat"))
    _check("resolve 会话人格", (pid, note) == ("my_persona", ""))

    # 无 resolver 的假宿主：会话未绑定 → 默认人格
    ctx2 = _Ctx(_ConvMgr(None, None), _PersonaMgr(default_name="default"))
    pid2, _ = _run(po.resolve_session_persona_id(ctx2, "chat:1:u1", "telegram"))
    _check("resolve 回退默认人格", pid2 == "default")

    # [%None] 哨兵 → 不能当目标，回退默认
    ctx3 = _Ctx(_ConvMgr("cid2", "[%None]"), _PersonaMgr(default_name="default"))
    pid3, _ = _run(po.resolve_session_persona_id(ctx3, "chat:1:u1", "telegram"))
    _check("resolve [%None] 走默认", pid3 == "default")

    # 都没有 → None + 说明
    ctx4 = _Ctx(_ConvMgr(None, None), _PersonaMgr(default_name=None))
    pid4, note4 = _run(po.resolve_session_persona_id(ctx4, "chat:1:u1", "telegram"))
    _check("resolve 全空给说明", pid4 is None and "未绑定" in note4, note4)

    # resolver 路径直通（含 [%None] 清洗）
    ctx5 = _Ctx(_ConvMgr("cid3", "[%None]"), _PersonaMgrWithResolver())
    pid5, _ = _run(po.resolve_session_persona_id(ctx5, "chat:1:u1", "telegram"))
    _check("resolve 哨兵被清洗", pid5 is None)

    # webchat 专用默认人格不可编辑
    pn = "webchat"
    ctx6 = _Ctx(_ConvMgr("cid4", None), _PersonaMgrWithResolver())
    pid6, _ = _run(po.resolve_session_persona_id(ctx6, "chat:1:u1", pn))
    _check(
        "resolve webchat 默认不可编辑", pid6 is None
    )  # resolver 返回 None → 哨兵清洗


def test_update_persona_prompt() -> None:
    mgr = _PersonaMgr({"p1": _Persona("old prompt")})
    old = _run(po.update_persona_prompt(mgr, "p1", "new prompt"))
    _check("update 返回旧值", old == "old prompt")
    _check(
        "update 只传 system_prompt",
        mgr.calls == [("p1", {"system_prompt": "new prompt"})],
        mgr.calls,
    )
    _check("update 写入生效", mgr._personas["p1"].system_prompt == "new prompt")
    try:
        _run(po.update_persona_prompt(mgr, "ghost", "x"))
        _check("update 不存在抛 ValueError", False)
    except ValueError:
        _check("update 不存在抛 ValueError", True)


def test_read_prompt_file() -> None:
    p = SANDBOX / "prompt.txt"
    p.write_text("\ufeff\n  新人格提示词内容\n", encoding="utf-8")
    content = po.read_prompt_file(str(p), base_dir=str(SANDBOX / "workspace"))
    _check("read 去掉 BOM 与首尾空白", content == "新人格提示词内容", repr(content))

    # 相对路径基于 base_dir
    ws = SANDBOX / "workspace"
    ws.mkdir(exist_ok=True)
    (ws / "rel.txt").write_text("相对路径内容", encoding="utf-8")
    _check(
        "read 相对路径",
        po.read_prompt_file("rel.txt", base_dir=str(ws)) == "相对路径内容",
    )

    def _err(fn):
        try:
            fn()
            return None
        except po.PromptFileError as e:
            return str(e)

    msg = _err(lambda: po.read_prompt_file(str(SANDBOX / "nope.txt")))
    _check("read 不存在报错", msg is not None and "不存在" in msg)
    msg_dir = _err(lambda: po.read_prompt_file(str(SANDBOX)))
    _check("read 目录报错", msg_dir is not None and "不是文件" in msg_dir)
    big = SANDBOX / "big.txt"
    big.write_text("x" * 300, encoding="utf-8")
    msg_big = _err(lambda: po.read_prompt_file(str(big), max_bytes=100))
    _check("read 超限报错", msg_big is not None and "过大" in msg_big)
    empty = SANDBOX / "empty_ws.txt"
    empty.write_text("   \n\n  ", encoding="utf-8")
    msg_empty = _err(lambda: po.read_prompt_file(str(empty), max_bytes=1000))
    _check("read 全空白报错", msg_empty is not None and "内容为空" in msg_empty)
    msg_empty2 = _err(lambda: po.read_prompt_file("", base_dir=str(ws)))
    _check("read 空路径报错", msg_empty2 is not None and "路径为空" in msg_empty2)


def run_all() -> int:
    started = time.monotonic()
    tests = [
        test_normalize_levels,
        test_filter_and_format,
        test_collect_from_buffer_budget,
        test_capture_and_lifecycle,
        test_scan_log_files,
        test_resolve_persona_chain,
        test_update_persona_prompt,
        test_read_prompt_file,
    ]
    for fn in tests:
        t0 = time.monotonic()
        fn()
        print(f"  [ok] {fn.__name__} ({(time.monotonic() - t0) * 1000:.0f} ms)")
    print(
        f"\n全部通过：{len(PASS)} 项断言，{len(tests)} 个用例，"
        f"用时 {(time.monotonic() - started) * 1000:.0f} ms"
    )
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    try:
        sys.exit(run_all())
    except AssertionError as e:
        print(e)
        sys.exit(1)
    finally:
        # 清掉可能残留的 root handler，避免影响本机其它进程的测试
        try:
            lc.current.detach()
        except Exception:  # noqa: BLE001, S110 - 清扫兜底，失败无需记录
            pass
