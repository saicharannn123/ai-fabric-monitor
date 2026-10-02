import json
import re

from prometheus_client import CollectorRegistry, generate_latest

from fabric_exporter.backends import MockBackend
from fabric_exporter.collector import FabricCollector


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


def scrape(backend):
    reg = CollectorRegistry()
    reg.register(FabricCollector(backend))
    text = generate_latest(reg).decode()
    samples = {}
    for line in text.splitlines():
        if line and not line.startswith("#"):
            k, v = line.rsplit(" ", 1)
            samples[k] = float(v)
    return samples


def get(samples, name, **labels):
    hits = [
        v
        for k, v in samples.items()
        if k.startswith(name + "{") and all(re.search(rf'[{{,]{lk}="{lv}"', k) for lk, lv in labels.items())
    ]
    assert len(hits) == 1, (name, labels, hits)
    return hits[0]


def test_healthy_fabric(tmp_path):
    state = tmp_path / "mock.json"
    state.write_text("{}")
    clock = Clock()
    be = MockBackend(str(state), clock=clock)
    s = scrape(be)
    assert sum(v for k, v in s.items() if k.startswith("fabric_bgp_peer_up{")) == 8  # 4 links x 2 ends
    assert get(s, "fabric_route_ecmp_paths", device="leaf1", prefix="10.1.2.0/24") == 2
    assert get(s, "fabric_device_up", device="spine2") == 1


def test_counters_grow_and_split_across_ecmp(tmp_path):
    state = tmp_path / "mock.json"
    state.write_text("{}")
    clock = Clock()
    be = MockBackend(str(state), clock=clock)
    scrape(be)
    clock.t += 10
    s = scrape(be)
    up1 = get(s, "fabric_interface_tx_bytes_total", device="leaf1", interface="eth1")
    up2 = get(s, "fabric_interface_tx_bytes_total", device="leaf1", interface="eth2")
    host = get(s, "fabric_interface_rx_bytes_total", device="leaf1", interface="eth3")
    assert up1 > 0 and abs(up1 - up2) < 1  # even ECMP split
    assert abs((up1 + up2) - host) < 2  # what enters from the GPU host leaves on the uplinks


def test_link_failure_degrades_ecmp_and_drops_bgp(tmp_path):
    state = tmp_path / "mock.json"
    state.write_text(json.dumps({"down_links": ["spine1:eth1"], "error_links": ["leaf2:eth2"]}))
    clock = Clock()
    be = MockBackend(str(state), clock=clock)
    scrape(be)
    clock.t += 5
    s = scrape(be)
    assert get(s, "fabric_bgp_peer_up", device="leaf1", peer="eth1") == 0
    assert get(s, "fabric_bgp_peer_up", device="spine1", peer="eth1") == 0
    assert get(s, "fabric_interface_oper_up", device="leaf1", interface="eth1") == 0
    assert get(s, "fabric_route_ecmp_paths", device="leaf1", prefix="10.1.2.0/24") == 1
    assert get(s, "fabric_route_ecmp_paths", device="leaf2", prefix="10.1.1.0/24") == 1
    assert get(s, "fabric_interface_rx_errors_total", device="leaf2", interface="eth2") > 0
    # all traffic now rides spine2
    assert get(s, "fabric_interface_tx_bytes_total", device="leaf1", interface="eth1") == 0


def test_bad_device_does_not_break_scrape(tmp_path):
    class Flaky(MockBackend):
        def bgp_summary(self, device):
            if device == "spine2":
                raise RuntimeError("container not running")
            return super().bgp_summary(device)

    s = scrape(Flaky(str(tmp_path / "missing.json"), clock=Clock()))
    assert get(s, "fabric_device_up", device="spine2") == 0
    assert get(s, "fabric_device_up", device="leaf1") == 1
