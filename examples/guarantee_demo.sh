#!/usr/bin/env bash
# 抢修最低保障供水预演服务 —— 端到端调用示例（curl + jq/python3）。
# 两阶段：先判定全部保障量能否同时满足（不可行则给出缺口与瓶颈设施），
# 可行则保持全部保障量、按优先级逐点最大化额外供水（必要时重排先前流量）。
# 只读预演：不改变管网、阀位、复水计划或修订号。
# 用法：bash examples/guarantee_demo.sh [BASE_URL]
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
  --data @"$(dirname "$0")/sample_network.json" > /tmp/guarantee_load.json
pp < /tmp/guarantee_load.json
REV="$(get_field /tmp/guarantee_load.json revision)"
echo "(当前修订号 revision=$REV)"

echo
echo "== 2. 保障可行：D1=4/D2=2/D3=1 全部保障，再按优先级最大化额外供水 =="
curl -sS -X POST "$BASE/api/guarantee/preview" \
  -H 'Content-Type: application/json' \
  -d "{
        \"revision\": $REV,
        \"guarantees\": [
          {\"demand_id\": \"D1\", \"amount\": 4},
          {\"demand_id\": \"D2\", \"amount\": 2},
          {\"demand_id\": \"D3\", \"amount\": 1}
        ]
      }" | pp

echo
echo "== 3. 保障不可行：D1=10/D2=8/D3=6 超出可供能力，返回缺口与瓶颈设施（不返回部分方案） =="
curl -sS -X POST "$BASE/api/guarantee/preview" \
  -H 'Content-Type: application/json' \
  -d "{
        \"revision\": $REV,
        \"guarantees\": [
          {\"demand_id\": \"D1\", \"amount\": 10},
          {\"demand_id\": \"D2\", \"amount\": 8},
          {\"demand_id\": \"D3\", \"amount\": 6}
        ]
      }" | pp

echo
echo "== 4. 保障量超需求（D1: 10.5 > 10）：400，定位 guarantees[0].amount =="
curl -sS -o /tmp/resp.json -w "HTTP %{http_code}\n" -X POST "$BASE/api/guarantee/preview" \
  -H 'Content-Type: application/json' \
  -d "{
        \"revision\": $REV,
        \"guarantees\": [
          {\"demand_id\": \"D1\", \"amount\": 10.5},
          {\"demand_id\": \"D2\", \"amount\": 2},
          {\"demand_id\": \"D3\", \"amount\": 1}
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
          {\"demand_id\": \"GHOST\", \"amount\": 1},
          {\"demand_id\": \"S1\",    \"amount\": 1},
          {\"demand_id\": \"D1\",    \"amount\": 1},
          {\"demand_id\": \"D2\",    \"amount\": 1},
          {\"demand_id\": \"D2\",    \"amount\": 2}
        ]
      }"
pp < /tmp/resp.json

echo
echo "== 6. 过期修订号（阀变后仍携带旧 revision=$REV）：409，不返回混合版本结果 =="
curl -sS -X POST "$BASE/api/valves/change" \
  -H 'Content-Type: application/json' \
  -d '{"changes": [{"valve_id": "V3", "action": "close"}]}' > /dev/null
curl -sS -o /tmp/resp.json -w "HTTP %{http_code}\n" -X POST "$BASE/api/guarantee/preview" \
  -H 'Content-Type: application/json' \
  -d "{
        \"revision\": $REV,
        \"guarantees\": [
          {\"demand_id\": \"D1\", \"amount\": 4},
          {\"demand_id\": \"D2\", \"amount\": 2},
          {\"demand_id\": \"D3\", \"amount\": 1}
        ]
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
