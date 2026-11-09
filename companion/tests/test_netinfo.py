"""netinfo.lan_ip 单测:monkeypatch socket 覆盖回环-only 与有 LAN IP 两种情形。"""
from codey import netinfo


def test_lan_ip_returns_lan_when_present(monkeypatch):
    monkeypatch.setattr(netinfo.socket, "gethostname", lambda: "host")
    monkeypatch.setattr(netinfo.socket, "gethostbyname_ex",
                        lambda h: ("host", [], ["127.0.0.1", "192.168.1.42"]))
    assert netinfo.lan_ip() == "192.168.1.42"


def test_lan_ip_loopback_only_falls_back(monkeypatch):
    monkeypatch.setattr(netinfo.socket, "gethostname", lambda: "host")
    monkeypatch.setattr(netinfo.socket, "gethostbyname_ex",
                        lambda h: ("host", [], ["127.0.0.1"]))
    assert netinfo.lan_ip() == "127.0.0.1"


def test_lan_ip_skips_virtual_adapters(monkeypatch):
    # Windows 常见:VirtualBox / WSL(vEthernet)/ Clash TUN / Tailscale 与真实网卡并存
    monkeypatch.setattr(netinfo.socket, "gethostname", lambda: "host")
    monkeypatch.setattr(netinfo.socket, "gethostbyname_ex", lambda h: (
        "host", [], ["192.168.56.1", "172.19.128.1", "10.0.20.102", "198.18.0.1", "100.77.218.1"]))
    assert netinfo.lan_ip() == "10.0.20.102"
    assert netinfo.lan_ips() == ["10.0.20.102", "172.19.128.1"]


def test_lan_ip_prefers_home_lan(monkeypatch):
    monkeypatch.setattr(netinfo.socket, "gethostname", lambda: "host")
    monkeypatch.setattr(netinfo.socket, "gethostbyname_ex",
                        lambda h: ("host", [], ["172.20.0.1", "192.168.1.8"]))
    assert netinfo.lan_ip() == "192.168.1.8"


def test_lan_ip_swallows_errors(monkeypatch):
    def boom(_):
        raise OSError("no dns")
    monkeypatch.setattr(netinfo.socket, "gethostname", lambda: "host")
    monkeypatch.setattr(netinfo.socket, "gethostbyname_ex", boom)
    assert netinfo.lan_ip() == "127.0.0.1"
