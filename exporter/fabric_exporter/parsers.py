"""Pure parsing functions: turn raw FRR / iproute2 JSON into flat metric records.

Kept free of I/O so they can be unit-tested against captured device output.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Interfaces we never want to report (loopback, docker mgmt, kernel tunnels).
IGNORED_INTERFACES = {"lo", "eth0"}
IGNORED_PREFIXES = ("gre", "gretap", "erspan", "ip6", "sit", "tunl", "ip_vti", "ip6_vti")


@dataclass
class BgpPeer:
    peer: str  # neighbor key: interface name for unnumbered, IP otherwise
    remote_hostname: str
    remote_as: int
    state: str
    uptime_seconds: float
    prefixes_received: int
    prefixes_sent: int

    @property
    def up(self) -> bool:
        return self.state == "Established"


@dataclass
class InterfaceStats:
    name: str
    oper_up: bool
    mtu: int
    rx_bytes: int
    tx_bytes: int
    rx_packets: int
    tx_packets: int
    rx_errors: int
    tx_errors: int
    rx_dropped: int
    tx_dropped: int


@dataclass
class RouteInfo:
    prefix: str
    protocol: str
    active_nexthops: int
    nexthop_interfaces: list[str] = field(default_factory=list)


def parse_bgp_summary(data: dict[str, Any]) -> list[BgpPeer]:
    """Parse `vtysh -c 'show bgp summary json'` (FRR 8.x / 9.x)."""
    af = data.get("ipv4Unicast") or data  # older FRR returns the AF dict directly
    peers = []
    for name, p in (af.get("peers") or {}).items():
        peers.append(
            BgpPeer(
                peer=name,
                remote_hostname=p.get("hostname", ""),
                remote_as=int(p.get("remoteAs", 0)),
                state=p.get("state", "Unknown"),
                uptime_seconds=float(p.get("peerUptimeMsec", 0)) / 1000.0,
                prefixes_received=int(p.get("pfxRcd", 0)),
                prefixes_sent=int(p.get("pfxSnt", 0)),
            )
        )
    return peers


def parse_ip_link(data: list[dict[str, Any]]) -> list[InterfaceStats]:
    """Parse `ip -s -j link show`."""
    out = []
    for link in data:
        name = link.get("ifname", "")
        if name in IGNORED_INTERFACES or name.startswith(IGNORED_PREFIXES):
            continue
        stats = link.get("stats64") or link.get("stats") or {}
        rx, tx = stats.get("rx", {}), stats.get("tx", {})
        flags = link.get("flags", [])
        oper = link.get("operstate", "UNKNOWN")
        # veth pairs report operstate UP; fall back to LOWER_UP flag when UNKNOWN
        oper_up = oper == "UP" or (oper == "UNKNOWN" and "LOWER_UP" in flags)
        out.append(
            InterfaceStats(
                name=name,
                oper_up=oper_up,
                mtu=int(link.get("mtu", 0)),
                rx_bytes=int(rx.get("bytes", 0)),
                tx_bytes=int(tx.get("bytes", 0)),
                rx_packets=int(rx.get("packets", 0)),
                tx_packets=int(tx.get("packets", 0)),
                rx_errors=int(rx.get("errors", 0)),
                tx_errors=int(tx.get("errors", 0)),
                rx_dropped=int(rx.get("dropped", 0)),
                tx_dropped=int(tx.get("dropped", 0)),
            )
        )
    return out


def parse_ip_route(data: dict[str, Any], protocols: tuple[str, ...] = ("bgp",)) -> list[RouteInfo]:
    """Parse `vtysh -c 'show ip route json'`, keeping selected routes of given protocols.

    The number of active next hops per prefix is the ECMP width - the key
    health signal for a Clos fabric (and for GPU all-reduce traffic that
    depends on it).
    """
    out = []
    for prefix, entries in data.items():
        for r in entries:
            if r.get("protocol") not in protocols or not r.get("selected"):
                continue
            nhs = [nh for nh in r.get("nexthops", []) if nh.get("active")]
            out.append(
                RouteInfo(
                    prefix=prefix,
                    protocol=r["protocol"],
                    active_nexthops=len(nhs),
                    nexthop_interfaces=sorted({nh.get("interfaceName", "") for nh in nhs}),
                )
            )
    return out
