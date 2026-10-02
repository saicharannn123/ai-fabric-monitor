#!/usr/bin/env bash
# Push GPU-style east-west traffic across the real lab fabric with iperf3.
# 8 parallel streams -> different L4 ports -> ECMP spreads them over both spines.
#   scripts/traffic.sh [seconds] [streams]
set -euo pipefail
LAB=ai-fabric; SECS="${1:-120}"; STREAMS="${2:-8}"
docker exec -d "clab-${LAB}-gpu-host2" iperf3 -s -D
sleep 1
echo "gpu-host1 -> gpu-host2: ${STREAMS} streams for ${SECS}s (watch the Grafana uplink panel)"
docker exec "clab-${LAB}-gpu-host1" iperf3 -c 10.1.2.10 -P "$STREAMS" -t "$SECS" -i 10
