"""Windows 平台实现(ctypes,仅标准库):进程表 / 监听端口 / 剪贴板+按键 / 窗口前置。

macOS 走 ps/lsof/pbcopy/osascript;Windows 没有这些命令,由本模块提供等价能力。
只在 os.name == "nt" 时被导入。
"""
import ctypes
import os
import time
from ctypes import wintypes

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)
iphlpapi = ctypes.WinDLL("iphlpapi")

# ---------------- 进程表:pid / ppid / rss / cpu% / exe ----------------

TH32CS_SNAPPROCESS = 0x2
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]


class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]


kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
kernel32.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESS_MEMORY_COUNTERS),
                                             wintypes.DWORD]


def _ft(ft):
    return (ft.dwHighDateTime << 32) | ft.dwLowDateTime     # 100ns 单位


def snapshot():
    """Toolhelp 快照 -> {pid: (ppid, exe)}。"""
    out = {}
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == INVALID_HANDLE_VALUE:
        return out
    try:
        e = PROCESSENTRY32W()
        e.dwSize = ctypes.sizeof(e)
        ok = kernel32.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            out[e.th32ProcessID] = (e.th32ParentProcessID, e.szExeFile)
            ok = kernel32.Process32NextW(snap, ctypes.byref(e))
    finally:
        kernel32.CloseHandle(snap)
    return out


def _times_and_rss(pid):
    """(创建时间, 累计 CPU 时间[100ns], RSS 字节);无权限/已退出返回 None。"""
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        c, x, k, u = (wintypes.FILETIME() for _ in range(4))
        if not kernel32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(x), ctypes.byref(k), ctypes.byref(u)):
            return None
        pmc = PROCESS_MEMORY_COUNTERS()
        pmc.cb = ctypes.sizeof(pmc)
        rss = pmc.WorkingSetSize if kernel32.K32GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb) else 0
        return _ft(c), _ft(k) + _ft(u), rss
    finally:
        kernel32.CloseHandle(h)


# 上一次采样:{pid: (创建时间, cpu_100ns, monotonic 秒)}。cpu% 需两次采样之差,首次为 0。
_cpu_prev = {}


def process_map():
    """与 proctree.parse_ps 同构:{pid: {pid, ppid, rss_kb, cpu, comm}}。cpu 为单核百分比(同 ps pcpu)。"""
    global _cpu_prev
    now = time.monotonic()
    cur, out = {}, {}
    for pid, (ppid, exe) in snapshot().items():
        info = {"pid": pid, "ppid": ppid, "rss_kb": 0, "cpu": 0.0, "comm": exe}
        tr = _times_and_rss(pid) if pid else None
        if tr:
            created, cpu, rss = tr
            info["rss_kb"] = rss // 1024
            prev = _cpu_prev.get(pid)
            if prev and prev[0] == created and now > prev[2]:
                info["cpu"] = round(max(0.0, (cpu - prev[1]) / 1e7 / (now - prev[2]) * 100), 1)
            cur[pid] = (created, cpu, now)
        out[pid] = info
    _cpu_prev = cur
    return out


# ---------------- 监听端口:GetExtendedTcpTable(不解析本地化的 netstat 文本) ----------------

AF_INET, AF_INET6 = 2, 23
TCP_TABLE_OWNER_PID_LISTENER = 3
ERROR_INSUFFICIENT_BUFFER = 122

iphlpapi.GetExtendedTcpTable.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), wintypes.BOOL,
                                         wintypes.ULONG, ctypes.c_int, wintypes.ULONG]


def _tcp_table(family):
    size = wintypes.DWORD(0)
    buf = None
    for _ in range(4):                                        # 表在两次调用间可能变大,重试几次
        rc = iphlpapi.GetExtendedTcpTable(buf, ctypes.byref(size), False, family,
                                          TCP_TABLE_OWNER_PID_LISTENER, 0)
        if rc == 0:
            return buf
        if rc != ERROR_INSUFFICIENT_BUFFER:
            return None
        buf = ctypes.create_string_buffer(size.value)
    return None


def listen_ports():
    """{pid: [port]}(IPv4 + IPv6 LISTEN),与 proctree.parse_lsof_listen 同构。"""
    ports = {}
    # 行结构:IPv4 MIB_TCPROW_OWNER_PID = 6 个 DWORD(端口在第 3 个);
    #         IPv6 MIB_TCP6ROW_OWNER_PID = 16B 地址 + scope + port + 16B + scope + port + state + pid。
    for family, row_size, port_off, pid_off in ((AF_INET, 24, 8, 20), (AF_INET6, 56, 20, 52)):
        buf = _tcp_table(family)
        if buf is None:
            continue
        raw = buf.raw
        n = int.from_bytes(raw[0:4], "little")
        for i in range(n):
            base = 4 + i * row_size
            row = raw[base:base + row_size]
            if len(row) < row_size:
                break
            port = int.from_bytes(row[port_off:port_off + 2], "big")   # 网络字节序,低 16 位
            pid = int.from_bytes(row[pid_off:pid_off + 4], "little")
            lst = ports.setdefault(pid, [])
            if port not in lst:
                lst.append(port)
    return ports


# ---------------- 剪贴板 + 模拟按键(语音转写粘贴) ----------------

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
KEYEVENTF_KEYUP = 0x0002
VK_BACK, VK_RETURN, VK_CONTROL, VK_MENU = 0x08, 0x0D, 0x11, 0x12

kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.SetClipboardData.restype = wintypes.HANDLE
user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_size_t]


def set_clipboard(text):
    data = (str(text) + "\0").encode("utf-16-le")
    for _ in range(10):                                       # 剪贴板可能被别的进程短暂占用
        if user32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        raise OSError("OpenClipboard failed")
    try:
        user32.EmptyClipboard()
        h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not h:
            raise OSError("GlobalAlloc failed")
        p = kernel32.GlobalLock(h)
        ctypes.memmove(p, data, len(data))
        kernel32.GlobalUnlock(h)
        if not user32.SetClipboardData(CF_UNICODETEXT, h):
            kernel32.GlobalFree(h)                            # 成功时所有权归系统,失败才自己释放
            raise OSError("SetClipboardData failed")
    finally:
        user32.CloseClipboard()


def _key(vk, up=False):
    user32.keybd_event(vk, 0, KEYEVENTF_KEYUP if up else 0, 0)


def press(vk, ctrl=False):
    """按一下 vk(可带 Ctrl)。"""
    if ctrl:
        _key(VK_CONTROL)
    _key(vk)
    _key(vk, up=True)
    if ctrl:
        _key(VK_CONTROL, up=True)


# ---------------- 前台窗口 / 把会话所在窗口前置 ----------------

PROCESS_NAME_WIN32 = 0
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.GetWindow.restype = wintypes.HWND
user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.IsIconic.argtypes = [wintypes.HWND]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                ctypes.POINTER(wintypes.DWORD)]
GW_OWNER, SW_RESTORE = 4, 9


def exe_name(pid):
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        n = wintypes.DWORD(len(buf))
        if not kernel32.QueryFullProcessImageNameW(h, PROCESS_NAME_WIN32, buf, ctypes.byref(n)):
            return ""
        return os.path.basename(buf.value)
    finally:
        kernel32.CloseHandle(h)


def foreground_app():
    """当前前台窗口所属进程名(去掉 .exe);拿不到返回 "?"。"""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return "?"
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    name = exe_name(pid.value)
    return name[:-4] if name.lower().endswith(".exe") else (name or "?")


def _main_windows():
    """{pid: hwnd}:每个进程第一个可见、无 owner、有标题的顶层窗口。"""
    found = {}

    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd) and not user32.GetWindow(hwnd, GW_OWNER) \
                and user32.GetWindowTextLengthW(hwnd) > 0:
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            found.setdefault(pid.value, hwnd)
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return found


def focus_process_window(pid):
    """沿 pid 的祖先链找第一个有主窗口的进程(Windows Terminal / VS Code / Claude 桌面版等),
    把该窗口前置。只能切到窗口,不能选中其中的某个标签页。返回 (ok, reason)。"""
    procs = snapshot()
    wins = _main_windows()
    seen, cur = set(), pid
    while cur and cur not in seen and cur in procs:
        seen.add(cur)
        hwnd = wins.get(cur)
        if hwnd:
            if user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, SW_RESTORE)
            # 后台进程直接 SetForegroundWindow 会被系统拒绝;先模拟一次 Alt 键解除前台锁。
            _key(VK_MENU)
            _key(VK_MENU, up=True)
            ok = bool(user32.SetForegroundWindow(hwnd))
            return ok, "window:" + procs[cur][1] if ok else "set-foreground-denied"
        cur = procs[cur][0]
    return False, "window-not-found"
