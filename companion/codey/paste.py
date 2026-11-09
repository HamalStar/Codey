# companion/codey/paste.py
"""把转写文本打进当前聚焦窗口。
macOS:pbcopy + osascript Cmd+V。首次运行需在 系统设置→隐私与安全性→辅助功能 给运行 Python 的终端授权。
Windows:Win32 剪贴板 + 模拟 Ctrl+V(winapi)。无需额外授权;但目标窗口若以管理员身份运行,
普通权限的 companion 发出的按键会被系统(UIPI)丢弃。"""
import os
import subprocess

_WIN = os.name == "nt"


def _pbcopy(text):
    p = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
    p.communicate(text.encode("utf-8"))

def _osascript(script):
    subprocess.run(["osascript", "-e", script], check=False)

def paste_to_active_window(text):
    if not text:
        return
    if _WIN:
        from . import winapi
        winapi.set_clipboard(text)
        winapi.press(ord("V"), ctrl=True)
        return
    _pbcopy(text)
    _osascript('tell application "System Events" to keystroke "v" using command down')

def press_enter():
    if _WIN:
        from . import winapi
        winapi.press(winapi.VK_RETURN)
        return
    _osascript('tell application "System Events" to keystroke return')

def clear_input():
    if _WIN:
        from . import winapi
        winapi.press(ord("A"), ctrl=True)
        winapi.press(winapi.VK_BACK)
        return
    _osascript('tell application "System Events"\n'
               '  keystroke "a" using command down\n'
               '  key code 51\n'
               'end tell')

def get_active_app():
    if _WIN:
        try:
            from . import winapi
            return winapi.foreground_app()
        except Exception:
            return "?"
    try:
        out = subprocess.check_output(
            ["osascript", "-e",
             'tell application "System Events" to name of first application process whose frontmost is true'],
            stderr=subprocess.DEVNULL, timeout=2)
        return out.decode("utf-8").strip()
    except Exception:
        return "?"
