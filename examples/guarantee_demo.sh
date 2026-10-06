#!/usr/bin/env bash
# 抢修最低保障供水预演 —— 端到端调用示例（curl + jq/python3）。
# 先做保障量联合可行性判定；不可行只返回缺口与瓶颈设施（不出部分方案）；
# 可行则在保持所有保障量前提下按优先级逐点最大化额外供水（允许重排先前流量）。
# 只读预演：不改变管网、阀位、复水计划或修订号。
# 用法：bash examples/guarantee_demo.sh [BASE_URL]
set -euo pipefail

BASE="${1:-http://localhost:8080}"
JQ="$(command -v jq || true)"
pp() { if [ -n "$JQ" ]; then jq .; else cat; fi; }

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
  --data @"$(dirname "$0")/sample_network.json" > /tmp/guarantee_load.json
pp < /tmp/guarantee_load.json
REV="$(get_field /tmp/guarantee_load.json revision)"
echo "(当前修订号 revision=$REV)"

echo
echo "== 2. 可行保障：D2=4、D3=2（S2 能力 6 刚好覆盖）；保持保障并逐点最大化 =="
echo "   期望 D1=10（高优先级先最大化），D2=4（P3 容量 4），D3=2（S2 剩余）"
curl -sS -X POST "$BASE/api/guarantee/preview" \
  -H 'Content-Type: application/json' \
  -d "{
        \"revision\": $REV,
        \"guarantees\": [
          {\"demand_id\": \"D1\", \"guarantee\": 0},
          {\"demand_id\": \"D2\", \"guarantee\": 4},
          {\"demand_id\": \"D3\", \"guarantee\": 2}
        ]
      }" | pp

echo
echo "== 3. 不可行：D2 保障 5 超过共享管段 P3 容量 4 —— 只回缺口与瓶颈，不出部分方案 =="
curl -sS -X POST "$BASE/api/guarantee/preview" \
  -H 'Content-Type: application/json' \
  -d "{
        \"revision\": $REV,
        \"guarantees\": [
          {\"demand_id\": \"D1\", \"guarantee\": 10},
          {\"demand_id\": \"D2\", \"guarantee\": 8},
          {\"demand_id\": \"D3\", \"guarantee\": 6}
        ]
      }" | pp

echo
echo "== 4. 保障量超过现有需求（D1: 11 > 10）：400，定位 guarantees[0].guarantee =="
curl -sS -o /tmp/resp.json -w "HTTP %{http_code}\n" -X POST "$BASE/api/guarantee/preview" \
  -H 'Content-Type: application/json' \
  -d "{
        \"revision\": $REV,
        \"guarantees\": [
          {\"demand_id\": \"D1\", \"guarantee\": 11},
          {\"demand_id\": \"D2\", \"guarantee\": 0},
          {\"demand_id\": \"D3\", \"guarantee\": 0}
        ]
      }"
pp < /tmp/resp.json

echo
echo "== 5. 未知/错类/重复/缺失条目：400，逐条带定位，整单拒绝 =="
curl -sS -o /tmp/resp.json -w "HTTP %{http_code}\n" -X POST "$BASE/api/guarantee/preview" \
  -H 'Content-Type: application/json' \
  -d "{
        \"revision\": $REV,
        \"guarantees\": [
          {\"demand_id\": \"GHOST\", \"guarantee\": 1},
          {\"demand_id\": \"S1\",    \"guarantee\": 1},
          {\"demand_id\": \"D1\",    \"guarantee\": 1},
          {\"demand_id\": \"D2\",    \"guarantee\": 1},
          {\"demand_id\": \"D2\",    \"guarantee\": 2}
        ]
      }"
pp < /tmp/resp.json

echo
echo "== 6. 负数保障量：422 =="
curl -sS -o /tmp/resp.json -w "HTTP %{http_code}\n" -X POST "$BASE/api/guarantee/preview" \
  -H 'Content-Type: application/json' \
  -d "{
        \"revision\": $REV,
        \"guarantees\": [
          {\"demand_id\": \"D1\", \"guarantee\": -1},
          {\"demand_id\": \"D2\", \"guarantee\": 0},
          {\"demand_id\": \"D3\", \"guarantee\": 0}
        ]
      }"
pp < /tmp/resp.json

echo
echo "== 7. 过期修订号（阀变后仍携带旧 revision=$REV）：409，不返回混合版本结果 =="
curl -sS -X POST "$BASE/api/valves/change" \
  -H 'Content-Type: application/json' \
  -d '{"changes": [{"valve_id": "V3", "action": "close"}]}' > /dev/null
curl -sS -o /tmp/resp.json -w "HTTP %{http_code}\n" -X POST "$BASE/api/guarantee/preview" \
  -H 'Content-Type: application/json' \
  -d "{
        \"revision\": $REV,
        \"guarantees\": [
          {\"demand_id\": \"D1\", \"guarantee\": 0},
          {\"demand_id\": \"D2\", \"guarantee\": 4},
          {\"demand_id\": \"D3\", \"guarantee\": 2}
        ]
      }"
pp < /tmp/resp.json
# 顺带演示：当前阀位下 V3 关闭使 D2 被隔离，保障 D2=3 不可行
REV2="$(curl -sS "$BASE/api/status" | { if [ -n "$JQ" ]; then jq -r .revision; else python3 -c 'import json,sys;print(json.load(sys.stdin)["revision"])'; fi; })"
echo "== 7b. 取新修订号 revision=$REV2；V3 关闭时保障 D2=3 -> feasible=false / ISOLATED =="
curl -sS -X POST "$BASE/api/guarantee/preview" \
  -H 'Content-Type: application/json' \
  -d "{
        \"revision\": $REV2,
        \"guarantees\": [
          {\"demand_id\": \"D1\", \"guarantee\": 0},
          {\"demand_id\": \"D2\", \"guarantee\": 3},
          {\"demand_id\": \"D3\", \"guarantee\": 0}
        ]
      }" | pp
# 恢复阀位
curl -sS -X POST "$BASE/api/valves/change" \
  -H 'Content-Type: application/json' \
  -d '{"changes": [{"valve_id": "V3", "action": "open"}]}' > /dev/null
echo "(V3 已恢复开启)"

echo
echo "== 8. 确认预演不写入：管网与修订号均未被本轮预演改变 =="
curl -sS "$BASE/api/network" | { if [ -n "$JQ" ]; then jq '{revision, initialized}'; else cat; fi; }
