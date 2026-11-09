"""discovery 单测:信标内容 + 按网卡逐个广播(假 socket,不真正发包)。"""
import json

from codey import discovery


def test_beacon_payload(monkeypatch):
    monkeypatch.setattr(discovery.socket, "gethostname", lambda: "DESKTOP-X")
    j = json.loads(discovery.beacon(8787, 8788))
    assert j == {"codey": 1, "port": 8787, "asr": 8788, "host": "DESKTOP-X"}


class _FakeSock:
    log = []

    def __init__(self, *a):
        self.ip = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def setsockopt(self, *a):
        pass

    def bind(self, addr):
        if addr[0] == "10.9.9.9":
            raise OSError("adapter gone")
        self.ip = addr[0]

    def sendto(self, data, addr):
        _FakeSock.log.append((self.ip, addr, data))


def test_send_once_per_interface_skips_failures(monkeypatch):
    _FakeSock.log = []
    monkeypatch.setattr(discovery.socket, "socket", _FakeSock)
    n = discovery.send_once(b"x", ["192.168.1.8", "10.9.9.9", "172.19.128.1"])
    assert n == 2
    assert [(ip, addr) for ip, addr, _ in _FakeSock.log] == [
        ("192.168.1.8", ("255.255.255.255", discovery.PORT)),
        ("172.19.128.1", ("255.255.255.255", discovery.PORT)),
    ]


def test_enabled_env(monkeypatch):
    monkeypatch.delenv("CODEY_DISCOVERY", raising=False)
    assert discovery.enabled()
    monkeypatch.setenv("CODEY_DISCOVERY", "0")
    assert not discovery.enabled()
