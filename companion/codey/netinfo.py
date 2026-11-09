"""网络信息小工具:本机 LAN IP(仅主机名解析,不对外发包)。"""
import ipaddress
import socket

# 常见虚拟/隧道网卡网段:不是手表能连到的局域网地址。
_VIRTUAL = [ipaddress.ip_network(n) for n in (
    "198.18.0.0/15",     # Clash / sing-box 等 TUN(fake-ip)
    "100.64.0.0/10",     # CGNAT:Tailscale 等
    "192.168.56.0/24",   # VirtualBox host-only 默认
)]


def _rank(ip):
    """越小越像真实局域网:192.168.x > 10.x > 172.16/12(Hyper-V/WSL/Docker 常用)> 其它。"""
    a = ipaddress.ip_address(ip)
    if a.is_loopback or a.is_link_local or any(a in n for n in _VIRTUAL):
        return None
    if a in ipaddress.ip_network("192.168.0.0/16"):
        return 0
    if a in ipaddress.ip_network("10.0.0.0/8"):
        return 1
    if a in ipaddress.ip_network("172.16.0.0/12"):
        return 2
    return 3


def lan_ips():
    """本机候选 LAN IP,按可能性排序(Windows 上常同时有 VPN/虚拟机/WSL 网卡)。"""
    try:
        ips = socket.gethostbyname_ex(socket.gethostname())[2]
    except Exception:
        return []
    ranked = [(r, i, ip) for i, ip in enumerate(ips) if (r := _rank(ip)) is not None]
    return [ip for _, _, ip in sorted(ranked)]


def lan_ip():
    """本机 LAN IP;仅解析主机名,失败回 "127.0.0.1"。"""
    ips = lan_ips()
    return ips[0] if ips else "127.0.0.1"
