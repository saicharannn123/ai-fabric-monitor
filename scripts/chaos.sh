#!/usr/bin/env bash
# Failure injection for the AI fabric - works against the real containerlab lab
# or the mock fabric used by the demo.
#
#   scripts/chaos.sh link-down leaf1:eth1     # take a leaf-spine link down
#   scripts/chaos.sh link-up   leaf1:eth1
#   scripts/chaos.sh errors    leaf2:eth2     # dirty optic (mock) / 5% loss (lab)
#   scripts/chaos.sh flap      spine2:eth2 5  # flap a link 5 times
#   scripts/chaos.sh clear                    # restore everything
#   scripts/chaos.sh status
set -euo pipefail
LAB=ai-fabric
STATE="$(cd "$(dirname "$0")/.." && pwd)/monitoring/state/mock.json"

lab_running() { docker ps --format '{{.Names}}' 2>/dev/null | grep -q "^clab-${LAB}-leaf1$"; }

mock_edit() { # key op endpoint
  python3 - "$STATE" "$@" <<'PY'
import json, sys
path, key, op, ep = sys.argv[1], sys.argv[2], sys.argv[3], (sys.argv[4] if len(sys.argv) > 4 else None)
try:
    st = json.load(open(path))
except (OSError, ValueError):
    st = {}
st.setdefault("down_links", []); st.setdefault("error_links", [])
if op == "add" and ep not in st[key]: st[key].append(ep)
if op == "del" and ep in st[key]: st[key].remove(ep)
if op == "clear": st = {"down_links": [], "error_links": []}
json.dump(st, open(path, "w"))
print(json.dumps(st))
PY
}

parse_ep() { NODE="${1%%:*}"; IFACE="${1##*:}"; }

case "${1:-}" in
  link-down|link-up)
    parse_ep "$2"; state="${1#link-}"
    if lab_running; then
      docker exec "clab-${LAB}-${NODE}" ip link set dev "$IFACE" "$state" && echo "lab: $NODE $IFACE $state"
    else
      [[ $state == down ]] && mock_edit down_links add "$2" || mock_edit down_links del "$2"
    fi ;;
  errors)
    parse_ep "$2"
    if lab_running; then
      docker exec "clab-${LAB}-${NODE}" tc qdisc replace dev "$IFACE" root netem loss 5% \
        && echo "lab: 5% loss on $NODE $IFACE"
    else
      mock_edit error_links add "$2"
    fi ;;
  flap)
    for _ in $(seq "${3:-5}"); do "$0" link-down "$2"; sleep 20; "$0" link-up "$2"; sleep 20; done ;;
  clear)
    if lab_running; then
      for n in spine1 spine2 leaf1 leaf2; do for i in eth1 eth2; do
        docker exec "clab-${LAB}-$n" ip link set dev "$i" up
        docker exec "clab-${LAB}-$n" tc qdisc del dev "$i" root 2>/dev/null || true
      done; done; echo "lab: all fabric links restored"
    else
      mock_edit down_links clear
    fi ;;
  status)
    if lab_running; then docker exec "clab-${LAB}-leaf1" vtysh -c "show bgp summary"
    else cat "$STATE"; echo; fi ;;
  *) sed -n '2,12p' "$0"; exit 1 ;;
esac
