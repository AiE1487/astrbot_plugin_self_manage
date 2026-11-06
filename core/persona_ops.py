"""人格提示词的解析与更新。

对照 AstrBot 4.28/4.29 源码核实过的语义：
- ``persona_manager.get_persona(persona_id)`` 查库中人格记录，不存在抛
  ``ValueError``（core/persona_mgr.py）。
- ``persona_manager.update_persona(persona_id, system_prompt=...)`` 走 db
  层：仅当参数不是 None/NOT_GIVEN 时才写库，因此只传 ``system_prompt``
  不会动 begin_dialogs / tools / skills 等其它人格字段。
- ``persona_manager.resolve_selected_persona(umo=..., platform_name=...,
  conversation_persona_id=...)``（较新版本才有）是宿主解析"当前会话人格"
  的权威实现，能处理会话级强制人格、``[%None]`` 与 webchat 专用默认人格
  ``_chatui_default_``；老版本回退为手动链：会话 persona_id →
  ``get_default_persona_v3``。
- 会话侧人格写 ``[%None]`` 与 webchat 专用 ``_chatui_default_`` 时不落库，
  不能作为更新目标。
"""

from __future__ import annotations

from pathlib import Path

from astrbot.api import logger

# 不能作为人格更新目标的取值（webchat 专用默认人格与“关人格”哨兵）
NO_PERSONA_SENTINELS: frozenset[str] = frozenset({"[%None]", "_chatui_default_", ""})


class PromptFileError(Exception):
    """提示词文件不可读/超限。消息中文、可直接作为 LLM 工具返回给 agent。"""


def _dict_get(obj, key: str, default=None):
    """兼容 dict-like Personality（dict 子类）与 ORM 属性访问。"""
    if obj is None:
        return default
    try:
        return obj.get(key, default)
    except AttributeError:
        return getattr(obj, key, default)


def _clean_persona_id(
    persona_id: str | None, webchat_special: bool = False
) -> tuple[str | None, str]:
    """过滤不能当作更新目标的取值，返回 (可编辑 id 或 None, 说明)。"""
    if webchat_special:
        return (
            None,
            "当前会话用的是 webchat 专用默认人格（_chatui_default_），它不存人格数据库，无法编辑",
        )
    if not persona_id or persona_id in NO_PERSONA_SENTINELS:
        return None, (
            f"当前会话未绑定人格（persona_id={persona_id!r}），"
            "请先在 WebUI（人格情境页）为该会话选择或新建人格"
        )
    return persona_id, ""


async def resolve_session_persona_id(
    context,
    unified_msg_origin: str,
    platform_name: str = "",
) -> tuple[str | None, str]:
    """解析“当前会话最终生效的人格 id”。

    返回 ``(persona_id, note)``。persona_id 为 None 时 note 说明原因。
    """
    persona_mgr = context.persona_manager
    conv_persona_id: str | None = None
    try:
        conv_mgr = context.conversation_manager
        conv_id = await conv_mgr.get_curr_conversation_id(unified_msg_origin)
        if conv_id:
            conversation = await conv_mgr.get_conversation(unified_msg_origin, conv_id)
            conv_persona_id = getattr(conversation, "persona_id", None)
    except Exception as e:  # noqa: BLE001 - 会话记录缺失不应阻塞默认人格回退
        logger.warning(f"[self_manage] 获取会话人格 id 失败：{e!r}")

    resolver = getattr(persona_mgr, "resolve_selected_persona", None)
    if resolver is not None:
        persona_id, _persona_obj, _force, webchat_special = await resolver(
            umo=unified_msg_origin,
            conversation_persona_id=conv_persona_id,
            platform_name=platform_name,
        )
        return _clean_persona_id(persona_id, webchat_special)

    # 老版本回退：会话 persona_id → 该会话默认人格
    if conv_persona_id and conv_persona_id not in NO_PERSONA_SENTINELS:
        return _clean_persona_id(conv_persona_id)
    try:
        default_persona = await persona_mgr.get_default_persona_v3(
            umo=unified_msg_origin
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[self_manage] 获取默认人格失败：{e!r}")
        default_persona = None
    fallback_id = _dict_get(default_persona, "name")
    if fallback_id:
        return _clean_persona_id(fallback_id)
    return None, "当前会话未绑定人格，也未找到可用的默认人格"


async def current_persona_prompt(persona_mgr, persona_id: str) -> str:
    """读取人格记录当前的 system_prompt（不存在则 ValueError 上抛）。"""
    persona = await persona_mgr.get_persona(persona_id)
    return _dict_get(persona, "system_prompt") or ""


async def update_persona_prompt(
    persona_mgr,
    persona_id: str,
    new_prompt: str,
) -> str:
    """把人格记录的 system_prompt 换成 new_prompt，返回旧提示词。

    只传 system_prompt（db 层对 None 字段跳过更新），不会触碰
    begin_dialogs / tools / skills 等其它人格字段。
    """
    old_prompt = await current_persona_prompt(persona_mgr, persona_id)
    await persona_mgr.update_persona(persona_id, system_prompt=new_prompt)
    return old_prompt


def read_prompt_file(
    file_path: str,
    base_dir: str = "",
    max_bytes: int = 100 * 1024,
) -> str:
    """读取提示词文件并做安全校验，返回提示词文本。

    相对路径基于 ``base_dir``（AstrBot 工作根目录）解析。失败抛
    ``PromptFileError``，消息中文、可直接作为 LLM 工具返回给 agent。
    """
    path = (file_path or "").strip()
    if not path:
        raise PromptFileError("文件路径为空：请提供提示词文件在 bot 主机上的完整路径。")
    try:
        resolved = Path(path).expanduser()
        if not resolved.is_absolute():
            resolved = (Path(base_dir) / resolved) if base_dir else resolved
        resolved = resolved.resolve()
    except OSError as e:
        raise PromptFileError(f"路径无法解析：{file_path!r}（{e}）") from e

    if not resolved.exists():
        raise PromptFileError(f"文件不存在：{resolved}（路径必须在 bot 主机上可访问）")
    if not resolved.is_file():
        raise PromptFileError(f"该路径不是文件：{resolved}")

    try:
        size = resolved.stat().st_size
    except OSError as e:
        raise PromptFileError(f"读取文件失败：{resolved}（{e}）") from e
    if size == 0:
        raise PromptFileError(f"文件为空：{resolved}")
    if size > max_bytes:
        raise PromptFileError(
            f"文件过大：{resolved}（{size} 字节，超过上限 {max_bytes} 字节）。"
            "请缩小提示词内容，或调整插件配置 persona_prompt_max_kb。"
        )
    try:
        content = resolved.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise PromptFileError(f"文件不是 UTF-8 编码：{resolved}") from None
    except OSError as e:
        raise PromptFileError(f"读取文件失败：{resolved}（{e}）") from None

    content = content.lstrip("\ufeff").strip()
    if not content:
        raise PromptFileError(f"文件内容为空（去掉首尾空白后）：{resolved}")
    return content
