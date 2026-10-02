"""Entry point: `python -m fabric_exporter`.

Environment variables
  EXPORTER_MODE     docker | mock            (default: mock)
  EXPORTER_PORT     HTTP port                (default: 9342)
  CLAB_LAB_NAME     containerlab lab name    (default: ai-fabric)
  FABRIC_DEVICES    comma-separated nodes    (default: spine1,spine2,leaf1,leaf2)
  LINK_SPEED_BPS    nominal link speed       (default: 10e9)
  MOCK_STATE_FILE   failure-injection file   (default: /state/mock.json)
"""

from __future__ import annotations

import logging
import os
import time

from prometheus_client import REGISTRY, start_http_server

from .backends import DockerBackend, MockBackend
from .collector import FabricCollector


def build_backend():
    mode = os.environ.get("EXPORTER_MODE", "mock").lower()
    if mode == "docker":
        devices = os.environ.get("FABRIC_DEVICES", "spine1,spine2,leaf1,leaf2").split(",")
        return DockerBackend(os.environ.get("CLAB_LAB_NAME", "ai-fabric"), [d.strip() for d in devices])
    if mode == "mock":
        return MockBackend()
    raise SystemExit(f"unknown EXPORTER_MODE={mode!r} (use docker or mock)")


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    backend = build_backend()
    REGISTRY.register(FabricCollector(backend, float(os.environ.get("LINK_SPEED_BPS", "10e9"))))
    port = int(os.environ.get("EXPORTER_PORT", "9342"))
    start_http_server(port)
    logging.info("fabric exporter (%s) listening on :%d", type(backend).__name__, port)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
