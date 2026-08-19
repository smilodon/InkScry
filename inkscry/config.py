"""配置加载：读取项目根目录 .env（无第三方依赖的极简解析）。"""

from __future__ import annotations

import os
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path | None = None) -> None:
    """把 .env 中的 KEY=VALUE 写入环境变量（不覆盖已有变量）。

    未加引号的值支持行内注释（空白 + # 起）；加引号的值原样保留。
    """
    env_file = path or PROJECT_ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if value[:1] in ("\"", "'"):
            q = value[0]
            end = value.find(q, 1)
            value = value[1:end] if end > 0 else value.strip(q)
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
        os.environ.setdefault(key, value)


def device_name() -> str | None:
    """广播名只来自 .env / 环境变量（不再内置默认值）。"""
    return os.environ.get("INKSCRY_DEVICE_NAME") or None


def device_address() -> str | None:
    return os.environ.get("INKSCRY_DEVICE_ADDRESS") or None
