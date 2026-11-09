"""局域网自动发现:定时从每块候选网卡广播 UDP 信标,手表收到后以包的源 IP 作为 Companion 地址。

手表侧不用填 IP:mDNS 被拦(公司网)、电脑换了 DHCP 地址、或固件里没有配网页时都能找到。
信标只含端口与主机名(不含任何会话数据),只在局域网广播域内传播。
关闭:环境变量 CODEY_DISCOVERY=0;端口:CODEY_DISCOVERY_PORT(默认 8789,需与固件一致)。
"""
import json
import os
import socket
import time

from .netinfo import lan_ips

PORT = int(os.environ.get("CODEY_DISCOVERY_PORT") or 8789)
INTERVAL_S = 2.0          # 手表 resolve 时最多听 3s,2s 一发保证至少收到一个
IP_REFRESH_S = 30.0       # 网卡 / IP 变化(换 Wi-Fi、DHCP 续租)的重新探测周期


def enabled():
    return os.environ.get("CODEY_DISCOVERY", "1") != "0"


def beacon(http_port, asr_port):
    return json.dumps({"codey": 1, "port": http_port, "asr": asr_port,
                       "host": socket.gethostname()}, separators=(",", ":")).encode()


def send_once(payload, ips):
    """从每个本机 IP 各发一次受限广播(绑定源 IP → 走对应网卡)。返回成功发出的个数。"""
    sent = 0
    for ip in ips:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                s.bind((ip, 0))
                s.sendto(payload, ("255.255.255.255", PORT))
                sent += 1
        except OSError:
            pass                                  # 网卡刚断开 / 无广播权限:跳过这块,下轮再试
    return sent


def run(http_port, asr_port):
    """后台线程主循环:永不抛出。"""
    payload = beacon(http_port, asr_port)
    ips, ips_at = [], 0.0
    while True:
        now = time.monotonic()
        if not ips or now - ips_at >= IP_REFRESH_S:
            ips, ips_at = lan_ips(), now
        send_once(payload, ips)
        time.sleep(INTERVAL_S)
