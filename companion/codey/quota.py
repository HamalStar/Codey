"""账号级额度:Claude 读 statusline 截获的本地文件,Codex 读 codexbar 本地缓存
(没有 codexbar —— 如 Windows —— 则读 Codex 自己 rollout 里最近的 rate_limits)。零联网。"""
import json
import os
import time
from datetime import datetime, timedelta

from . import discover
from .codex_rollout import parse_codex_rollout
from .util import clamp_pct, parse_ts_ms

REAL_FRESH_MS = 30 * 60 * 1000
REAL_USAGE_FILE = os.path.join(os.path.expanduser("~"), ".claude", "codey-usage.json")
CODEX_HISTORY = os.path.join(os.path.expanduser("~"), "Library", "Application Support",
                             "com.steipete.codexbar", "history", "codex.json")


def read_real_usage():
    """Claude 真实限额(由 codey-statusline.sh 写入 ~/.claude/codey-usage.json)。"""
    try:
        with open(REAL_USAGE_FILE, encoding="utf-8") as fh:
            j = json.load(fh)
    except (OSError, ValueError):
        return None
    ts = j.get("ts")
    j["fresh"] = isinstance(ts, (int, float)) and (time.time() * 1000 - ts * 1000) < REAL_FRESH_MS
    return j


def end_of_week_epoch():
    """下周一 00:00(本地)epoch 秒。"""
    now = datetime.now()
    days = (7 - now.weekday()) % 7 or 7
    nxt = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=days)
    return int(nxt.timestamp())


def read_codex_rollout_usage(codex_root=None, scan=5):
    """Codex 额度兜底:最近几个 rollout 里最后一次 token_count 的 rate_limits。
    重置时间已过的窗口视为 0%(额度已刷新,只是还没有新的 rollout 记录)。"""
    root = codex_root or os.path.join(os.path.expanduser("~"), ".codex")
    now = int(time.time())
    for roll in discover.list_codex_rollouts(root, limit=scan):
        p = parse_codex_rollout(discover.read_rollout_text(roll["path"]))
        if p["five_hour_pct"] is None and p["weekly_pct"] is None:
            continue

        def win(pct, reset):
            if pct is None:
                return None
            reset = int(reset or 0)
            if reset and reset <= now:
                return {"pct": 0, "reset": 0}
            return {"pct": clamp_pct(pct), "reset": reset}

        return {"session": win(p["five_hour_pct"], p["five_hour_resets_at"]),
                "weekly": win(p["weekly_pct"], p["weekly_resets_at"]), "src": "rollout"}
    return None


def read_codex_usage():
    """Codex 额度:读 codexbar 菜单栏 App 的本地缓存 history/codex.json;没有则回落 rollout。"""
    try:
        with open(CODEX_HISTORY, encoding="utf-8") as fh:
            j = json.load(fh)
    except (OSError, ValueError):
        return read_codex_rollout_usage()
    try:
        acc = j["accounts"][j["preferredAccountKey"]]
    except (KeyError, TypeError):
        return None

    def pick(name):
        w = next((x for x in acc if isinstance(x, dict) and x.get("name") == name), None)
        entries = (w or {}).get("entries") or []
        if not entries:
            return None
        e = entries[-1]
        ms = parse_ts_ms(e.get("resetsAt"))
        return {"pct": clamp_pct(e.get("usedPercent")), "reset": ms // 1000 if ms else 0}

    return {"session": pick("session"), "weekly": pick("weekly"), "src": "codexbar"}
