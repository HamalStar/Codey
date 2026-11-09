"""Windows 适配 + 新版 Codex(.zst rollout / 数字 resets_at)相关的跨平台逻辑。
winapi 的真机用例仅在 Windows 上运行。"""
import io
import json
import os
import socket
import sys
import time
import types

import pytest

from codey import discover, focus, quota, usb_link
from codey.util import parse_ts_ms, path_parts, project_name

WIN = os.name == "nt"


# ---------------- util ----------------

def test_windows_paths():
    assert project_name(r"D:\B1\B1-Keel") == "B1-Keel"
    assert project_name("C:\\Users\\me\\code\\Codey\\") == "Codey"
    assert path_parts(r"C:\a/b\c.py") == ["C:", "a", "b", "c.py"]


def test_parse_ts_ms_accepts_epoch_numbers():
    assert parse_ts_ms(1793291207) == 1793291207000          # 秒(新版 Codex rate_limits)
    assert parse_ts_ms(1793291207123) == 1793291207123       # 已是毫秒
    assert parse_ts_ms(float("inf")) == 0
    assert parse_ts_ms(True) == 0
    assert parse_ts_ms("2026-06-03T12:00:00Z") > 0           # ISO 仍可用


# ---------------- Claude 会话发现:项目目录名对不上时按 sessionId 兜底 ----------------

def test_discover_falls_back_to_session_id_lookup(tmp_path):
    claude = tmp_path / ".claude"
    (claude / "sessions").mkdir(parents=True)
    proj = claude / "projects" / "C--very-long-path-truncated-abc123"   # 截断 + hash,encode_cwd 算不出
    proj.mkdir(parents=True)
    pid = os.getpid()
    (claude / "sessions" / f"{pid}.json").write_text(json.dumps(
        {"pid": pid, "sessionId": "sid-x", "cwd": "C:\\very\\long\\path", "startedAt": 1000}))
    (proj / "sid-x.jsonl").write_text("{}\n")
    out = discover.discover_claude_sessions([str(claude)], {pid})
    assert len(out) == 1 and out[0]["session_id"] == "sid-x"
    assert out[0]["transcript_path"] == str(proj / "sid-x.jsonl")


# ---------------- Codex rollout:.zst + 数字 resets_at ----------------

def _rollout_lines(five=61.0, week=10.0, reset5=None, reset7=None):
    now = int(time.time())
    return [
        {"type": "session_meta", "payload": {"id": "cx-1", "cwd": "D:\\proj\\web"}},
        {"type": "event_msg", "payload": {"type": "token_count", "info": {}, "rate_limits": {
            "primary": {"used_percent": five, "window_minutes": 300, "resets_at": reset5 or now + 3600},
            "secondary": {"used_percent": week, "window_minutes": 10080, "resets_at": reset7 or now + 86400}}}},
    ]


def _write_zst(path, lines, truncate_tail=False):
    zstd = pytest.importorskip("zstandard")
    raw = ("\n".join(json.dumps(x) for x in lines) + "\n").encode()
    data = zstd.ZstdCompressor().compress(raw)
    if truncate_tail:                     # 模拟正在写入:再追加半个帧
        data += zstd.ZstdCompressor().compress(b'{"type":"event_msg"}\n' * 50)[:20]
    path.write_bytes(data)


def test_list_and_read_zst_rollout(tmp_path):
    day = tmp_path / ".codex" / "sessions" / "2026" / "11" / "09"
    day.mkdir(parents=True)
    f = day / "rollout-2026-11-09T10-00-00-abc.jsonl.zst"
    _write_zst(f, _rollout_lines(), truncate_tail=True)
    rolls = discover.list_codex_rollouts(str(tmp_path / ".codex"))
    assert [os.path.basename(r["path"]) for r in rolls] == [f.name]
    text = discover.read_rollout_text(str(f))
    assert '"cx-1"' in text and "token_count" in text


def test_codex_usage_from_rollout(tmp_path):
    day = tmp_path / ".codex" / "sessions" / "2026" / "11" / "09"
    day.mkdir(parents=True)
    _write_zst(day / "rollout-a.jsonl.zst", _rollout_lines(five=61.4, week=10.0))
    u = quota.read_codex_rollout_usage(str(tmp_path / ".codex"))
    assert u["src"] == "rollout"
    assert u["session"]["pct"] == 61 and u["weekly"]["pct"] == 10
    assert u["session"]["reset"] > time.time()


def test_codex_usage_expired_window_reads_zero(tmp_path):
    day = tmp_path / ".codex" / "sessions" / "2026" / "11" / "09"
    day.mkdir(parents=True)
    past = int(time.time()) - 60
    (day / "rollout-b.jsonl").write_text(
        "\n".join(json.dumps(x) for x in _rollout_lines(five=90, week=40, reset5=past)))
    u = quota.read_codex_rollout_usage(str(tmp_path / ".codex"))
    assert u["session"] == {"pct": 0, "reset": 0}      # 5h 窗口已重置
    assert u["weekly"]["pct"] == 40


def test_codex_usage_none_without_rollouts(tmp_path):
    assert quota.read_codex_rollout_usage(str(tmp_path / ".codex")) is None


# ---------------- USB 串口:Windows 按 Espressif VID 选 COM 口 ----------------

def test_find_port_windows_picks_espressif_vid(monkeypatch):
    ports = [types.SimpleNamespace(device="COM3", vid=0x10C4),
             types.SimpleNamespace(device="COM7", vid=0x303A)]
    fake = types.SimpleNamespace(comports=lambda: ports)
    monkeypatch.setattr(usb_link, "_WIN", True)
    monkeypatch.setitem(sys.modules, "serial.tools.list_ports", fake)
    monkeypatch.setitem(sys.modules, "serial.tools", types.SimpleNamespace(list_ports=fake))
    monkeypatch.setitem(sys.modules, "serial", sys.modules.get("serial") or types.ModuleType("serial"))
    assert usb_link.find_port() == "COM7"


# ---------------- focus:Windows 走窗口前置,pane 恒为 None ----------------

def test_focus_windows_paths(monkeypatch):
    import codey
    fake = types.SimpleNamespace(focus_process_window=lambda pid: (True, f"window:{pid}"))
    monkeypatch.setattr(focus, "_WIN", True)
    monkeypatch.setattr(codey, "winapi", fake, raising=False)
    monkeypatch.setitem(sys.modules, "codey.winapi", fake)
    assert focus.focus_pid(1234) == (True, "window:1234")
    assert focus.focus_pid(0) == (False, "no-pid")
    assert focus.tty_for_pid(1234) is None
    assert focus.pane_for_pid(1234) is None


# ---------------- statusline(Python 版) ----------------

def test_statusline_capture_and_render(tmp_path, monkeypatch, capsys):
    import codey_statusline as sl
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("CODEY_STATUSLINE_CMD", raising=False)
    (tmp_path / ".claude").mkdir()
    payload = {"model": {"display_name": "Opus"},
               "rate_limits": {"five_hour": {"used_percentage": 42.4, "resets_at": 1793291207},
                               "seven_day": {"used_percentage": 18, "resets_at": 1793791790}}}
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(json.dumps(payload).encode())))
    sl.main()
    assert capsys.readouterr().out == "Opus · 5h 42% · 7d 18%"
    saved = json.loads((tmp_path / ".claude" / "codey-usage.json").read_text())
    assert saved["five_hour"] == {"used_percentage": 42.4, "resets_at": 1793291207}
    assert saved["model"] == "Opus"


def test_statusline_bad_input_still_renders(tmp_path, monkeypatch, capsys):
    import codey_statusline as sl
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("CODEY_STATUSLINE_CMD", raising=False)
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(b"not json")))
    sl.main()                                   # 没有 ~/.claude 目录:截获失败也不能影响输出
    assert capsys.readouterr().out == "Claude"


# ---------------- winapi 真机(仅 Windows) ----------------

@pytest.mark.skipif(not WIN, reason="Windows only")
def test_winapi_process_map_has_self_and_parent():
    from codey import winapi
    pm = winapi.process_map()
    me = pm[os.getpid()]
    assert me["ppid"] == os.getppid()
    assert me["rss_kb"] > 0 and me["comm"].lower().startswith("python")


@pytest.mark.skipif(not WIN, reason="Windows only")
def test_winapi_listen_ports_sees_our_sockets():
    from codey import winapi
    s4 = socket.socket(socket.AF_INET); s4.bind(("127.0.0.1", 0)); s4.listen()
    s6 = socket.socket(socket.AF_INET6); s6.bind(("::1", 0)); s6.listen()
    try:
        ports = winapi.listen_ports().get(os.getpid(), [])
        assert s4.getsockname()[1] in ports
        assert s6.getsockname()[1] in ports
    finally:
        s4.close(); s6.close()


@pytest.mark.skipif(not WIN, reason="Windows only")
def test_winapi_cpu_sampling_detects_busy_process():
    from codey import winapi
    winapi.process_map()
    end = time.time() + 0.5
    while time.time() < end:                    # 烧 0.5s CPU
        pass
    assert winapi.process_map()[os.getpid()]["cpu"] > 20
