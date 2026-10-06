#!/usr/bin/env bash
# 多时段有限储水调度预演服务 —— 端到端调用示例（curl + jq/python3）。
# 逐期：补水至库容（超出计溢出）-> 按现有阀位/单向/共享容量/优先级分配 -> 出水扣减储水。
# 只读预演：不改变管网、阀位、复水计划或修订号。
# 用法：bash examples/schedule_demo.sh [BASE_URL]
set -euo pipefail

BASE="${1:-http://localhost:8080}"
JQ="$(command -v jq || true)"
pp() { if [ -n "$JQ" ]; then jq .; else cat; fi; }

# 不依赖 jq 也能取 revision（优先 jq，回退 python3，再回退 grep/sed）
get_field() {  # $1=JSON文件 $2=字段名
  if [ -n "$JQ" ]; then jq -r ".$2" "$1"
  elif command -v python3 >/dev/null 2>&1; then
    python3 -c "import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])" "$1" "$2"
  else
    grep -o "\"$2\": *[0-9-]*" "$1" | head -1 | sed 's/.*: *//'
  fi
}

echo "== 0. 健康检查 =="
curl -sS "$BASE/health"; echo

echo "== 1. 整批录入示例管网（原子覆盖），取得当前修订号 =="
curl -sS -X PUT "$BASE/api/network" \
  -H 'Content-Type: application/json' \
  --data @"$(dirname "$0")/sample_network.json" > /tmp/schedule_load.json
pp < /tmp/schedule_load.json
REV="$(get_field /tmp/schedule_load.json revision)"
echo "(当前修订号 revision=$REV)"

echo
echo "== 2. 两期调度预演：期1 补水（S2 超库容溢出 1），期2 不补水吃储水 =="
curl -sS -X POST "$BASE/api/schedule/preview" \
  -H 'Content-Type: application/json' \
  -d "{
        \"revision\": $REV,
        \"sources\": [
          {\"source_id\": \"S1\", \"initial_storage\": 8, \"storage_capacity\": 20},
          {\"source_id\": \"S2\", \"initial_storage\": 5, \"storage_capacity\": 10}
        ],
        \"periods\": [
          {
            \"replenishment\": [{\"source_id\": \"S1\", \"amount\": 5}, {\"source_id\": \"S2\", \"amount\": 6}],
            \"demands\": [{\"demand_id\": \"D1\", \"amount\": 10}, {\"demand_id\": \"D2\", \"amount\": 8}, {\"demand_id\": \"D3\", \"amount\": 6}]
          },
          {
            \"replenishment\": [{\"source_id\": \"S1\", \"amount\": 0}, {\"source_id\": \"S2\", \"amount\": 0}],
            \"demands\": [{\"demand_id\": \"D1\", \"amount\": 10}, {\"demand_id\": \"D2\", \"amount\": 8}, {\"demand_id\": \"D3\", \"amount\": 6}]
          }
        ]
      }" | pp

echo
echo "== 3. 初始储水超库容（S1: 21 > 20）：400，定位 sources[0].initial_storage =="
curl -sS -o /tmp/resp.json -w "HTTP %{http_code}\n" -X POST "$BASE/api/schedule/preview" \
  -H 'Content-Type: application/json' \
  -d "{
        \"revision\": $REV,
        \"sources\": [
          {\"source_id\": \"S1\", \"initial_storage\": 21, \"storage_capacity\": 20},
          {\"source_id\": \"S2\", \"initial_storage\": 5,  \"storage_capacity\": 10}
        ],
        \"periods\": [{
          \"replenishment\": [{\"source_id\": \"S1\", \"amount\": 0}, {\"source_id\": \"S2\", \"amount\": 0}],
          \"demands\": [{\"demand_id\": \"D1\", \"amount\": 1}, {\"demand_id\": \"D2\", \"amount\": 1}, {\"demand_id\": \"D3\", \"amount\": 1}]
        }]
      }"
pp < /tmp/resp.json

echo
echo "== 4. 未知/错类/重复/缺失条目：400，逐条带定位，整单拒绝 =="
curl -sS -o /tmp/resp.json -w "HTTP %{http_code}\n" -X POST "$BASE/api/schedule/preview" \
  -H 'Content-Type: application/json' \
  -d "{
        \"revision\": $REV,
        \"sources\": [
          {\"source_id\": \"GHOST\", \"initial_storage\": 1, \"storage_capacity\": 5},
          {\"source_id\": \"D1\",    \"initial_storage\": 1, \"storage_capacity\": 5},
          {\"source_id\": \"S1\",    \"initial_storage\": 1, \"storage_capacity\": 5}
        ],
        \"periods\": [{
          \"replenishment\": [{\"source_id\": \"S1\", \"amount\": 1}, {\"source_id\": \"S1\", \"amount\": 2}],
          \"demands\": [{\"demand_id\": \"D1\", \"amount\": 1}, {\"demand_id\": \"P1\", \"amount\": 1}]
        }]
      }"
pp < /tmp/resp.json

echo
echo "== 5. 空时段（periods: []）：422 =="
curl -sS -o /tmp/resp.json -w "HTTP %{http_code}\n" -X POST "$BASE/api/schedule/preview" \
  -H 'Content-Type: application/json' \
  -d "{\"revision\": $REV, \"sources\": [], \"periods\": []}"
pp < /tmp/resp.json

echo
echo "== 6. 过期修订号（阀变后仍携带旧 revision=$REV）：409，不返回混合版本结果 =="
curl -sS -X POST "$BASE/api/valves/change" \
  -H 'Content-Type: application/json' \
  -d '{"changes": [{"valve_id": "V3", "action": "close"}]}' > /dev/null
curl -sS -o /tmp/resp.json -w "HTTP %{http_code}\n" -X POST "$BASE/api/schedule/preview" \
  -H 'Content-Type: application/json' \
  -d "{
        \"revision\": $REV,
        \"sources\": [
          {\"source_id\": \"S1\", \"initial_storage\": 8, \"storage_capacity\": 20},
          {\"source_id\": \"S2\", \"initial_storage\": 5, \"storage_capacity\": 10}
        ],
        \"periods\": [{
          \"replenishment\": [{\"source_id\": \"S1\", \"amount\": 0}, {\"source_id\": \"S2\", \"amount\": 0}],
          \"demands\": [{\"demand_id\": \"D1\", \"amount\": 1}, {\"demand_id\": \"D2\", \"amount\": 1}, {\"demand_id\": \"D3\", \"amount\": 1}]
        }]
      }"
pp < /tmp/resp.json
# 恢复阀位
curl -sS -X POST "$BASE/api/valves/change" \
  -H 'Content-Type: application/json' \
  -d '{"changes": [{"valve_id": "V3", "action": "open"}]}' > /dev/null
echo "(V3 已恢复开启)"

echo
echo "== 7. 确认预演不写入：管网与修订号均未被本轮预演改变 =="
curl -sS "$BASE/api/network" | pp
