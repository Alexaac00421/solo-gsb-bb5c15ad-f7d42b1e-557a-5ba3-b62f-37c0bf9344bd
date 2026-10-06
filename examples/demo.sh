#!/usr/bin/env bash
# 供水管网隔离预演服务 —— 端到端调用示例（curl + jq）。
# 用法：bash examples/demo.sh [BASE_URL]
set -euo pipefail

BASE="${1:-http://localhost:8080}"
JQ="$(command -v jq || true)"
pp() { if [ -n "$JQ" ]; then jq .; else cat; fi; }

echo "== 0. 健康检查 =="
curl -sS "$BASE/health"; echo

echo "== 1. 整批录入示例管网（原子覆盖） =="
curl -sS -X PUT "$BASE/api/network" \
  -H 'Content-Type: application/json' \
  --data @"$(dirname "$0")/sample_network.json" | pp

echo
echo "== 2. 查询当前真实状态下的分派（D1 优先；D2 受 P3=4 限制） =="
curl -sS "$BASE/api/status" | pp

echo
echo "== 3. 预演：拟关闭 V1（D1 将被隔离）—— 不写入任何状态 =="
curl -sS -X POST "$BASE/api/drill" \
  -H 'Content-Type: application/json' \
  -d '{"close_valves": ["V1"]}' | pp

echo
echo "== 4. 阀门整批变更：打开 V5（连接互锁水源 S1/S2，必须被拒绝，409） =="
curl -sS -o /tmp/resp.json -w "HTTP %{http_code}\n" -X POST "$BASE/api/valves/change" \
  -H 'Content-Type: application/json' \
  -d '{"changes": [{"valve_id": "V5", "action": "open"}]}'
pp < /tmp/resp.json

echo
echo "== 5. 非法阀门编号整批变更（400，定位到 changes[0].valve_id） =="
curl -sS -o /tmp/resp.json -w "HTTP %{http_code}\n" -X POST "$BASE/api/valves/change" \
  -H 'Content-Type: application/json' \
  -d '{"changes": [{"valve_id": "V404", "action": "close"}]}'
pp < /tmp/resp.json

echo
echo "== 6. 合法整批变更：关闭 V3，随后恢复 =="
curl -sS -X POST "$BASE/api/valves/change" \
  -H 'Content-Type: application/json' \
  -d '{"changes": [{"valve_id": "V3", "action": "close"}]}' | pp
curl -sS -X POST "$BASE/api/valves/change" \
  -H 'Content-Type: application/json' \
  -d '{"changes": [{"valve_id": "V3", "action": "open"}]}' > /dev/null
echo "(V3 已恢复开启)"
