import types

import pytest

from codey import paste


@pytest.fixture
def mac(monkeypatch):
    monkeypatch.setattr(paste, "_WIN", False)


@pytest.fixture
def win(monkeypatch):
    """假 winapi:记录剪贴板/按键调用,不碰真实剪贴板和键盘。"""
    import codey
    calls = []
    fake = types.SimpleNamespace(
        VK_RETURN=0x0D, VK_BACK=0x08,
        set_clipboard=lambda t: calls.append(("clip", t)),
        press=lambda vk, ctrl=False: calls.append(("key", vk, ctrl)),
        foreground_app=lambda: "WindowsTerminal",
    )
    monkeypatch.setattr(paste, "_WIN", True)
    monkeypatch.setattr(codey, "winapi", fake, raising=False)
    monkeypatch.setitem(__import__("sys").modules, "codey.winapi", fake)
    return calls


def test_paste_sets_clipboard_then_cmd_v(monkeypatch, mac):
    calls = []
    monkeypatch.setattr(paste, "_pbcopy", lambda text: calls.append(("pbcopy", text)))
    monkeypatch.setattr(paste, "_osascript", lambda script: calls.append(("osa", script)))
    paste.paste_to_active_window("你好 world")
    assert calls[0] == ("pbcopy", "你好 world")
    assert calls[1][0] == "osa" and "keystroke \"v\"" in calls[1][1]

def test_paste_empty_is_noop(monkeypatch, mac):
    calls = []
    monkeypatch.setattr(paste, "_pbcopy", lambda t: calls.append(t))
    monkeypatch.setattr(paste, "_osascript", lambda s: calls.append(s))
    paste.paste_to_active_window("")
    assert calls == []

def test_press_enter_and_clear(monkeypatch, mac):
    scripts = []
    monkeypatch.setattr(paste, "_osascript", lambda s: scripts.append(s))
    paste.press_enter(); paste.clear_input()
    assert "keystroke return" in scripts[0]
    assert "keystroke \"a\" using command down" in scripts[1] and "key code 51" in scripts[1]

def test_windows_paste_uses_clipboard_then_ctrl_v(win):
    paste.paste_to_active_window("你好 world")
    assert win == [("clip", "你好 world"), ("key", ord("V"), True)]

def test_windows_empty_paste_is_noop(win):
    paste.paste_to_active_window("")
    assert win == []

def test_windows_enter_and_clear(win):
    paste.press_enter(); paste.clear_input()
    assert win == [("key", 0x0D, False), ("key", ord("A"), True), ("key", 0x08, False)]

def test_windows_active_app(win):
    assert paste.get_active_app() == "WindowsTerminal"
