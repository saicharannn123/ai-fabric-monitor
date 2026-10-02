"""Backends fetch raw JSON from fabric devices.

* DockerBackend - talks to containerlab FRR nodes via `docker exec`.
* MockBackend   - simulates the same 2x2 fabric (traffic, ECMP, failures) so the
                  whole monitoring stack can be demoed on any laptop.

Both return data in the *exact* shape FRR / iproute2 produce, so the same
parsers are exercised in demo mode and in the real lab.
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from typing import Any, Protocol

# Reads interface counters from sysfs and prints them in `ip -s -j link` format.
# Works in any container with a POSIX shell (no dependency on iproute2 JSON support).
SYSFS_LINK_SCRIPT = r"""
cd /sys/class/net || exit 1
printf '['; sep=''
for i in *; do
  st=$i/statistics
  printf '%s{"ifname":"%s","operstate":"%s","mtu":%s,"flags":[],"stats64":{"rx":{"bytes":%s,"packets":%s,"errors":%s,"dropped":%s},"tx":{"bytes":%s,"packets":%s,"errors":%s,"dropped":%s}}}' \
    "$sep" "$i" "$(tr a-z A-Z < $i/operstate)" "$(cat $i/mtu)" \
    $(cat $st/rx_bytes $st/rx_packets $st/rx_errors $st/rx_dropped $st/tx_bytes $st/tx_packets $st/tx_errors $st/tx_dropped)
  sep=','
done
printf ']'
"""


class Backend(Protocol):
    def devices(self) -> list[str]: ...
    def bgp_summary(self, device: str) -> dict[str, Any]: ...
    def ip_route(self, device: str) -> dict[str, Any]: ...
    def ip_link(self, device: str) -> list[dict[str, Any]]: ...


class DockerBackend:
    def __init__(self, lab: str, devices: list[str]):
        import docker  # imported lazily so mock mode needs no docker SDK

        self._client = docker.from_env()
        self._lab = lab
        self._devices = devices

    def devices(self) -> list[str]:
        return self._devices

    def _exec(self, device: str, cmd: list[str]) -> str:
        c = self._client.containers.get(f"clab-{self._lab}-{device}")
        rc, out = c.exec_run(cmd, demux=False)
        if rc != 0:
            raise RuntimeError(f"{device}: {' '.join(cmd)} exited {rc}: {out[:200]!r}")
        return out.decode()

    def bgp_summary(self, device: str) -> dict[str, Any]:
        return json.loads(self._exec(device, ["vtysh", "-c", "show bgp summary json"]))

    def ip_route(self, device: str) -> dict[str, Any]:
        return json.loads(self._exec(device, ["vtysh", "-c", "show ip route json"]))

    def ip_link(self, device: str) -> list[dict[str, Any]]:
        return json.loads(self._exec(device, ["sh", "-c", SYSFS_LINK_SCRIPT]))


# --------------------------------------------------------------------------- #
#  Mock fabric
# --------------------------------------------------------------------------- #
SPINES = {"spine1": ("10.0.0.1", 65000), "spine2": ("10.0.0.2", 65000)}
LEAVES = {
    "leaf1": ("10.0.0.11", 65101, "10.1.1.0/24"),
    "leaf2": ("10.0.0.12", 65102, "10.1.2.0/24"),
}
# (leaf, leaf_if, spine, spine_if)
FABRIC_LINKS = [
    ("leaf1", "eth1", "spine1", "eth1"),
    ("leaf1", "eth2", "spine2", "eth1"),
    ("leaf2", "eth1", "spine1", "eth2"),
    ("leaf2", "eth2", "spine2", "eth2"),
]
HOST_LINKS = {"leaf1": "eth3", "leaf2": "eth3"}
BASE_TRAFFIC_BPS = 6e9  # simulated GPU-to-GPU flow, bits/s


class MockBackend:
    """Deterministic fabric simulator.

    Failures are injected through a JSON state file (default /state/mock.json):
        {"down_links": ["leaf1:eth1"], "error_links": ["spine2:eth2"]}
    Any endpoint of a link may be named; the whole link goes down.
    """

    def __init__(self, state_file: str | None = None, clock=time.time):
        self._state_file = state_file or os.environ.get("MOCK_STATE_FILE", "/state/mock.json")
        self._clock = clock
        self._start = clock()
        self._last = self._start
        self._counters: dict[tuple[str, str], dict[str, float]] = {}
        self._up_since: dict[tuple[str, str], float] = {}
        self._lock = threading.Lock()

    # ---- state ----------------------------------------------------------- #
    def _state(self) -> dict[str, list[str]]:
        try:
            with open(self._state_file) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _link_up(self, leaf: str, leaf_if: str, spine: str, spine_if: str, down: set[str]) -> bool:
        return f"{leaf}:{leaf_if}" not in down and f"{spine}:{spine_if}" not in down

    def _uplinks(self, down: set[str]) -> dict[tuple[str, str], bool]:
        """(leaf, spine) -> link up?"""
        return {(lf, sp): self._link_up(lf, li, sp, si, down) for lf, li, sp, si in FABRIC_LINKS}

    def devices(self) -> list[str]:
        return [*SPINES, *LEAVES]

    # ---- traffic model --------------------------------------------------- #
    def _rates(self, now: float, down: set[str]) -> dict[tuple[str, str], tuple[float, float]]:
        """Per-interface (rx_bps, tx_bps) for the current instant."""
        up = self._uplinks(down)
        # bursty training-style traffic: base + sine wave + periodic all-reduce spike
        t = now - self._start
        load = BASE_TRAFFIC_BPS * (0.6 + 0.3 * math.sin(t / 60) + (0.35 if int(t) % 120 < 15 else 0))
        rates: dict[tuple[str, str], list[float]] = {}

        def add(dev, ifn, rx, tx):
            r = rates.setdefault((dev, ifn), [0.0, 0.0])
            r[0] += rx
            r[1] += tx

        for src, dst, bps in (("leaf1", "leaf2", load), ("leaf2", "leaf1", load * 0.4)):
            paths = [sp for sp in SPINES if up[(src, sp)] and up[(dst, sp)]]
            if not paths:
                continue
            add(src, HOST_LINKS[src], bps, 0)
            add(dst, HOST_LINKS[dst], 0, bps)
            share = bps / len(paths)
            for sp in paths:
                s_if = next(li for lf, li, s, _ in FABRIC_LINKS if lf == src and s == sp)
                d_if = next(li for lf, li, s, _ in FABRIC_LINKS if lf == dst and s == sp)
                sp_in = next(si for lf, _, s, si in FABRIC_LINKS if lf == src and s == sp)
                sp_out = next(si for lf, _, s, si in FABRIC_LINKS if lf == dst and s == sp)
                add(src, s_if, 0, share)
                add(sp, sp_in, share, 0)
                add(sp, sp_out, 0, share)
                add(dst, d_if, share, 0)
        return {k: (v[0], v[1]) for k, v in rates.items()}

    def _advance(self) -> tuple[float, set[str], set[str]]:
        with self._lock:
            now = self._clock()
            st = self._state()
            down, errs = set(st.get("down_links", [])), set(st.get("error_links", []))
            dt = max(0.0, now - self._last)
            rates = self._rates(now, down)
            for dev, ifn in self._all_interfaces():
                c = self._counters.setdefault(
                    (dev, ifn), {k: 0.0 for k in ("rxb", "txb", "rxp", "txp", "rxe", "txe")}
                )
                rx, tx = rates.get((dev, ifn), (0.0, 0.0))
                c["rxb"] += rx / 8 * dt
                c["txb"] += tx / 8 * dt
                c["rxp"] += rx / 8 / 4096 * dt  # ~4 KB jumbo-ish frames
                c["txp"] += tx / 8 / 4096 * dt
                if f"{dev}:{ifn}" in errs:
                    c["rxe"] += 25 * dt  # CRC errors from a "dirty optic"
            self._last = now
            return now, down, errs

    def _all_interfaces(self):
        for lf, li, sp, si in FABRIC_LINKS:
            yield lf, li
            yield sp, si
        for lf, hi in HOST_LINKS.items():
            yield lf, hi

    # ---- device views (FRR-shaped JSON) ----------------------------------- #
    def ip_link(self, device: str) -> list[dict[str, Any]]:
        now, down, _ = self._advance()
        up = self._uplinks(down)
        out = [{"ifname": "lo", "operstate": "UNKNOWN", "mtu": 65536, "flags": ["LOOPBACK"]}]
        for dev, ifn in self._all_interfaces():
            if dev != device:
                continue
            link = next(
                (
                    (lf, sp)
                    for lf, li, sp, si in FABRIC_LINKS
                    if (lf, li) == (dev, ifn) or (sp, si) == (dev, ifn)
                ),
                None,
            )
            is_up = up[link] if link else True
            c = self._counters[(dev, ifn)]
            out.append(
                {
                    "ifname": ifn,
                    "operstate": "UP" if is_up else "DOWN",
                    "mtu": 9500,
                    "flags": ["BROADCAST", "MULTICAST", "UP"] + (["LOWER_UP"] if is_up else []),
                    "stats64": {
                        "rx": {
                            "bytes": int(c["rxb"]),
                            "packets": int(c["rxp"]),
                            "errors": int(c["rxe"]),
                            "dropped": 0,
                        },
                        "tx": {
                            "bytes": int(c["txb"]),
                            "packets": int(c["txp"]),
                            "errors": int(c["txe"]),
                            "dropped": 0,
                        },
                    },
                }
            )
        return out

    def _peer_uptime(self, key: tuple[str, str], is_up: bool, now: float) -> float:
        if not is_up:
            self._up_since.pop(key, None)
            return 0.0
        return now - self._up_since.setdefault(key, now)

    def bgp_summary(self, device: str) -> dict[str, Any]:
        now, down, _ = self._advance()
        up = self._uplinks(down)
        peers = {}
        if device in LEAVES:
            for lf, li, sp, _ in FABRIC_LINKS:
                if lf != device:
                    continue
                ok = up[(lf, sp)]
                other_leaves_reachable = sum(up[(o, sp)] for o in LEAVES if o != lf)
                peers[li] = self._peer(
                    sp,
                    SPINES[sp][1],
                    ok,
                    1 + 2 * other_leaves_reachable,
                    3,
                    self._peer_uptime((device, li), ok, now),
                )
            rid, asn = LEAVES[device][0], LEAVES[device][1]
        else:
            for lf, _, sp, si in FABRIC_LINKS:
                if sp != device:
                    continue
                ok = up[(lf, sp)]
                peers[si] = self._peer(
                    lf,
                    LEAVES[lf][1],
                    ok,
                    2,
                    1 + 2 * (len(LEAVES) - 1),
                    self._peer_uptime((device, si), ok, now),
                )
            rid, asn = SPINES[device]
        return {
            "ipv4Unicast": {
                "routerId": rid,
                "as": asn,
                "peers": peers,
                "totalPeers": len(peers),
                "failedPeers": sum(p["state"] != "Established" for p in peers.values()),
            }
        }

    @staticmethod
    def _peer(hostname, remote_as, ok, rcvd, sent, uptime):
        return {
            "hostname": hostname,
            "remoteAs": remote_as,
            "state": "Established" if ok else "Active",
            "peerUptimeMsec": int(uptime * 1000),
            "pfxRcd": rcvd if ok else 0,
            "pfxSnt": sent if ok else 0,
        }

    def ip_route(self, device: str) -> dict[str, Any]:
        _, down, _ = self._advance()
        up = self._uplinks(down)
        routes: dict[str, list[str]] = {}
        if device in LEAVES:
            for lf, li, sp, _ in FABRIC_LINKS:
                if lf == device and up[(lf, sp)]:
                    routes.setdefault(SPINES[sp][0] + "/32", []).append(li)
            for other, (lo, _, subnet) in LEAVES.items():
                if other == device:
                    continue
                for lf, li, sp, _ in FABRIC_LINKS:
                    if lf == device and up[(lf, sp)] and up[(other, sp)]:
                        routes.setdefault(lo + "/32", []).append(li)
                        routes.setdefault(subnet, []).append(li)
        else:
            for lf, _, sp, si in FABRIC_LINKS:
                if sp == device and up[(lf, sp)]:
                    routes.setdefault(LEAVES[lf][0] + "/32", []).append(si)
                    routes.setdefault(LEAVES[lf][2], []).append(si)
        return {
            prefix: [
                {
                    "prefix": prefix,
                    "protocol": "bgp",
                    "selected": True,
                    "nexthops": [{"interfaceName": i, "active": True, "afi": "ipv6"} for i in ifs],
                }
            ]
            for prefix, ifs in routes.items()
        }
