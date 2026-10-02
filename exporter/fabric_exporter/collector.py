"""Prometheus collector: polls every fabric device at scrape time."""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor

from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily

from .backends import Backend
from .parsers import parse_bgp_summary, parse_ip_link, parse_ip_route

log = logging.getLogger(__name__)


def _role(device: str) -> str:
    return "spine" if device.startswith("spine") else "leaf" if device.startswith("leaf") else "other"


class FabricCollector:
    def __init__(self, backend: Backend, link_speed_bps: float = 10e9, workers: int = 8):
        self.backend = backend
        self.link_speed_bps = link_speed_bps
        self.pool = ThreadPoolExecutor(max_workers=workers)

    def _poll(self, device: str):
        t0 = time.monotonic()
        try:
            data = (
                parse_bgp_summary(self.backend.bgp_summary(device)),
                parse_ip_link(self.backend.ip_link(device)),
                parse_ip_route(self.backend.ip_route(device)),
            )
            return device, data, True, time.monotonic() - t0
        except Exception as exc:  # one bad device must not break the scrape
            log.warning("poll %s failed: %s", device, exc)
            return device, None, False, time.monotonic() - t0

    def collect(self):
        L = ["device", "role"]
        up = GaugeMetricFamily("fabric_device_up", "1 if the device was polled successfully", labels=L)
        dur = GaugeMetricFamily("fabric_device_poll_seconds", "Time taken to poll the device", labels=L)

        PL = L + ["peer", "remote_device", "remote_as"]
        bgp_up = GaugeMetricFamily("fabric_bgp_peer_up", "1 if the BGP session is Established", labels=PL)
        bgp_uptime = GaugeMetricFamily("fabric_bgp_peer_uptime_seconds", "BGP session uptime", labels=PL)
        bgp_rcv = GaugeMetricFamily("fabric_bgp_prefixes_received", "Prefixes accepted from peer", labels=PL)
        bgp_snt = GaugeMetricFamily("fabric_bgp_prefixes_sent", "Prefixes advertised to peer", labels=PL)

        IL = L + ["interface"]
        if_up = GaugeMetricFamily("fabric_interface_oper_up", "Interface operational state", labels=IL)
        if_mtu = GaugeMetricFamily("fabric_interface_mtu_bytes", "Interface MTU", labels=IL)
        if_speed = GaugeMetricFamily("fabric_interface_speed_bps", "Nominal link speed", labels=IL)
        counters = {
            k: CounterMetricFamily(f"fabric_interface_{k}", f"Interface {k.replace('_', ' ')}", labels=IL)
            for k in (
                "rx_bytes",
                "tx_bytes",
                "rx_packets",
                "tx_packets",
                "rx_errors",
                "tx_errors",
                "rx_dropped",
                "tx_dropped",
            )
        }

        RL = L + ["prefix"]
        ecmp = GaugeMetricFamily("fabric_route_ecmp_paths", "Active next hops for a BGP route", labels=RL)
        nroutes = GaugeMetricFamily("fabric_bgp_routes", "Number of installed BGP routes", labels=L)

        for device, data, ok, secs in self.pool.map(self._poll, self.backend.devices()):
            base = [device, _role(device)]
            up.add_metric(base, 1 if ok else 0)
            dur.add_metric(base, secs)
            if not ok:
                continue
            peers, links, routes = data
            for p in peers:
                lbl = base + [p.peer, p.remote_hostname, str(p.remote_as)]
                bgp_up.add_metric(lbl, 1 if p.up else 0)
                bgp_uptime.add_metric(lbl, p.uptime_seconds)
                bgp_rcv.add_metric(lbl, p.prefixes_received)
                bgp_snt.add_metric(lbl, p.prefixes_sent)
            for i in links:
                lbl = base + [i.name]
                if_up.add_metric(lbl, 1 if i.oper_up else 0)
                if_mtu.add_metric(lbl, i.mtu)
                if_speed.add_metric(lbl, self.link_speed_bps)
                for k, fam in counters.items():
                    fam.add_metric(lbl, getattr(i, k))
            for r in routes:
                ecmp.add_metric(base + [r.prefix], r.active_nexthops)
            nroutes.add_metric(base, len(routes))

        yield from (
            up,
            dur,
            bgp_up,
            bgp_uptime,
            bgp_rcv,
            bgp_snt,
            if_up,
            if_mtu,
            if_speed,
            *counters.values(),
            ecmp,
            nroutes,
        )
