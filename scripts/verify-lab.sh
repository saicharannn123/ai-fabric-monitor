#!/usr/bin/env bash
# Post-deploy checks: BGP sessions, ECMP routes and end-to-end reachability.
set -euo pipefail
LAB=ai-fabric; fail=0
for n in leaf1 leaf2 spine1 spine2; do
  est=$(docker exec "clab-${LAB}-$n" vtysh -c "show bgp summary json" \
        | python3 -c "import json,sys;p=json.load(sys.stdin)['ipv4Unicast']['peers'];print(sum(v['state']=='Established' for v in p.values()),len(p))")
  echo "$n BGP established/total: $est"; [[ ${est% *} == "${est#* }" ]] || fail=1
done
for pair in "leaf1 10.1.2.0/24" "leaf2 10.1.1.0/24"; do
  set -- $pair
  paths=$(docker exec "clab-${LAB}-$1" vtysh -c "show ip route $2 json" \
          | python3 -c "import json,sys;d=json.load(sys.stdin);print(sum(1 for r in d['$2'] for nh in r['nexthops'] if nh.get('active')))")
  echo "$1 ECMP paths to $2: $paths"; [[ $paths -ge 2 ]] || fail=1
done
docker exec "clab-${LAB}-gpu-host1" ping -c 3 -W 1 10.1.2.10 >/dev/null && echo "gpu-host1 -> gpu-host2: reachable" || fail=1
[[ $fail == 0 ]] && echo "FABRIC OK" || { echo "FABRIC CHECK FAILED"; exit 1; }
