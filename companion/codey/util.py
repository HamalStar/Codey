"""小工具:百分比夹取、码点截断、项目名、ISO 时间戳解析。纯函数。"""
import math
import re
import subprocess
from datetime import datetime

# Windows 上从无控制台的进程调 git 等命令时不弹黑框;其它平台为 0(无影响)。
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def path_parts(p):
    """按 / 和 \\ 切路径段(兼容 Windows 路径),去掉空段。"""
    return [s for s in re.split(r"[\\/]", str(p if p is not None else "")) if s]


def clamp_pct(x):
    """取整(四舍五入,半值向上,贴合 JS Math.round)并夹到 0..100。"""
    try:
        v = math.floor(float(x) + 0.5)
    except (TypeError, ValueError, OverflowError):   # inf -> OverflowError,nan -> ValueError
        v = 0
    return max(0, min(100, v))


def truncate(s, max_len):
    """按码点截断,超出加 '…'(总长含省略号 <= max_len)。"""
    s = str(s if s is not None else "")
    if len(s) <= max_len:
        return s
    return s[: max(0, max_len - 1)] + "…"


def project_name(cwd):
    """取 cwd 末段作项目名。"""
    parts = path_parts(cwd)
    return parts[-1] if parts else ""


def parse_ts_ms(ts):
    """ISO-8601 字符串 或 epoch 数字(秒/毫秒)-> epoch 毫秒;失败返回 0。
    新版 Codex rollout 的 rate_limits.resets_at 是 epoch 秒整数。"""
    if not ts:
        return 0
    if isinstance(ts, (int, float)) and not isinstance(ts, bool):
        if not math.isfinite(ts):
            return 0
        return int(ts) if ts > 1e12 else int(ts * 1000)
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000)
    except (ValueError, TypeError):
        return 0
