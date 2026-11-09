#!/usr/bin/env python3
"""Codey statusline(跨平台,仅标准库)—— codey-statusline.sh 的 Python 版,Windows 用这个。

Claude Code 把真实限额(与 /status 一致)以 JSON 经 stdin 交给 statusline 命令:
rate_limits.five_hour / .seven_day { used_percentage, resets_at }。这里把它写进
~/.claude/codey-usage.json(格式与 .sh 版相同),供 Companion 给手表下发精确百分比。

然后渲染状态栏:设了 CODEY_STATUSLINE_CMD 就把同一份 stdin 交给它、原样输出它的结果
(用来保留你原来的 statusline);否则输出一行 "模型 · 5h 42% · 7d 18%"。
截获失败从不影响状态栏渲染。

配置(~/.claude/settings.json):
  "statusLine": {"type": "command", "command": "py -3 C:\\path\\to\\companion\\codey_statusline.py"}
"""
import json
import os
import subprocess
import sys
import time


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def capture(j):
    r = j.get("rate_limits") or {}
    five, seven = r.get("five_hour") or {}, r.get("seven_day") or {}
    out = {
        "ts": int(time.time()),
        "model": (j.get("model") or {}).get("display_name"),
        "five_hour": {"used_percentage": _num(five.get("used_percentage")), "resets_at": _num(five.get("resets_at"))},
        "seven_day": {"used_percentage": _num(seven.get("used_percentage")), "resets_at": _num(seven.get("resets_at"))},
    }
    path = os.path.join(os.path.expanduser("~"), ".claude", "codey-usage.json")   # 同 quota.REAL_USAGE_FILE
    tmp = f"{path}.{os.getpid()}.tmp"                 # 每进程临时文件 -> 无 rename 竞争
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(out, fh)
    os.replace(tmp, path)
    return out


def render(raw, j, usage):
    cmd = os.environ.get("CODEY_STATUSLINE_CMD")
    if cmd:
        r = subprocess.run(cmd, shell=True, input=raw, capture_output=True, timeout=10)
        sys.stdout.buffer.write(r.stdout)
        return
    parts = [(j.get("model") or {}).get("display_name") or "Claude"]
    for label, key in (("5h", "five_hour"), ("7d", "seven_day")):
        pct = (usage or {}).get(key, {}).get("used_percentage")
        if pct is not None:
            parts.append(f"{label} {round(pct)}%")
    sys.stdout.write(" · ".join(parts))


def main():
    raw = sys.stdin.buffer.read()
    try:
        j = json.loads(raw or b"{}")
    except ValueError:
        j = {}
    usage = None
    try:
        usage = capture(j)
    except Exception:
        pass
    try:
        render(raw, j, usage)
    except Exception:
        sys.stdout.write("Claude")


if __name__ == "__main__":
    main()
