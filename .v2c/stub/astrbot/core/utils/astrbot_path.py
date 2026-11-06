"""stub/astrbot/core/utils/astrbot_path — 路径助手全部指向 .v2c/sandbox 沙盒。"""

from pathlib import Path

_BASE = Path(__file__).resolve().parents[4] / "sandbox"


def get_astrbot_root() -> str:
    return str(_BASE)


def get_astrbot_data_path() -> str:
    return str(_BASE / "data")


def get_astrbot_plugin_data_path() -> str:
    return str(_BASE / "plugin_data")
