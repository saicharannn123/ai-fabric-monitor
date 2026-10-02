import json
from pathlib import Path

from fabric_exporter.parsers import parse_bgp_summary, parse_ip_link, parse_ip_route

FIX = Path(__file__).parent.parent / "fixtures"


def load(name):
    return json.loads((FIX / name).read_text())


def test_bgp_summary_unnumbered_peers():
    peers = {p.peer: p for p in parse_bgp_summary(load("leaf1_bgp_summary.json"))}
    assert set(peers) == {"eth1", "eth2"}
    assert peers["eth1"].up and peers["eth1"].remote_hostname == "spine1"
    assert peers["eth1"].remote_as == 65000
    assert peers["eth1"].uptime_seconds == 1214.0
    assert peers["eth1"].prefixes_received == 3
    assert not peers["eth2"].up and peers["eth2"].state == "Active"


def test_bgp_summary_empty():
    assert parse_bgp_summary({}) == []


def test_ip_link_filters_mgmt_and_tunnels():
    links = {i.name: i for i in parse_ip_link(load("leaf1_ip_link.json"))}
    assert set(links) == {"eth1", "eth2", "eth3"}
    assert links["eth1"].oper_up and links["eth1"].tx_bytes == 912345678
    assert links["eth1"].tx_dropped == 2
    assert not links["eth2"].oper_up and links["eth2"].rx_errors == 17
    assert links["eth3"].mtu == 9500


def test_ip_route_counts_only_active_nexthops_of_selected_bgp_routes():
    routes = {r.prefix: r for r in parse_ip_route(load("leaf1_ip_route.json"))}
    assert set(routes) == {"10.0.0.1/32", "10.0.0.12/32", "10.1.2.0/24"}  # connected + unselected dropped
    assert routes["10.0.0.12/32"].active_nexthops == 1  # eth2 path inactive -> ECMP degraded
    assert routes["10.0.0.12/32"].nexthop_interfaces == ["eth1"]
