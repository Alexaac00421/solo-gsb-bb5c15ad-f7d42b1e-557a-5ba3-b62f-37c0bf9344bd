#!/usr/bin/env bash
# 抢修降额影响预演服务 —— 端到端调用示例（curl + jq/python3）。
# 以同一已提交管网快照分别计算“现状”与“降额”两种情景，只读、不写入。
# 用法：bash examples/derating_demo.sh [BASE_URL]
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
  --data @"$(dirname "$0")/sample_network.json" > /tmp/derating_load.json
pp < /tmp/derating_load.json
REV="$(get_field /tmp/derating_load.json revision)"
echo "(当前修订号 revision=$REV)"

echo
echo "== 2. 不降额（空清单）：降额情景应与现状完全一致 =="
curl -sS -X POST "$BASE/api/derating/preview" \
  -H 'Content-Type: application/json' \
  -d "{\"revision\": $REV}" | pp

echo
echo "== 3. 水源 S2 临时降额 6 -> 3（泵站检修）：看 D2/D3 获水变化 =="
curl -sS -X POST "$BASE/api/derating/preview" \
  -H 'Content-Type: application/json' \
  -d "{\"revision\": $REV, \"sources\": [{\"facility_id\": \"S2\", \"capacity\": 3}]}" | pp

echo
echo "== 4. 水源 S2 6->3 且管段 P3 4->2（泵站+管段同时降额） =="
curl -sS -X POST "$BASE/api/derating/preview" \
  -H 'Content-Type: application/json' \
  -d "{
        \"revision\": $REV,
        \"sources\": [{\"facility_id\": \"S2\", \"capacity\": 3}],
        \"pipes\":   [{\"facility_id\": \"P3\", \"capacity\": 2}]
      }" | pp

echo
echo "== 5. 管段临时上限 0（停输）：D2 经 P3 完全断流，D3 独占 S2 =="
curl -sS -X POST "$BASE/api/derating/preview" \
  -H 'Content-Type: application/json' \
  -d "{\"revision\": $REV, \"pipes\": [{\"facility_id\": \"P3\", \"capacity\": 0}]}" \
  | pp

echo
echo "== 6. 越额上限（S2=99 > 额定 6）：400，定位 sources[0].capacity =="
curl -sS -o /tmp/resp.json -w "HTTP %{http_code}\n" -X POST "$BASE/api/derating/preview" \
  -H 'Content-Type: application/json' \
  -d "{\"revision\": $REV, \"sources\": [{\"facility_id\": \"S2\", \"capacity\": 99}]}"
pp < /tmp/resp.json

echo
echo "== 7. 未知设施 / 错类别 / 重复条目：400，逐条带定位，整单拒绝 =="
curl -sS -o /tmp/resp.json -w "HTTP %{http_code}\n" -X POST "$BASE/api/derating/preview" \
  -H 'Content-Type: application/json' \
  -d "{
        \"revision\": $REV,
        \"sources\": [
          {\"facility_id\": \"GHOST\", \"capacity\": 1},
          {\"facility_id\": \"P1\",    \"capacity\": 1},
          {\"facility_id\": \"S2\",    \"capacity\": 2},
          {\"facility_id\": \"S2\",    \"capacity\": 1}
        ]
      }"
pp < /tmp/resp.json

echo
echo "== 8. 过期修订号（阀变后仍携带旧 revision=$REV）：409，不返回混合版本结果 =="
# 先制造一次阀位变更推进修订号
curl -sS -X POST "$BASE/api/valves/change" \
  -H 'Content-Type: application/json' \
  -d '{"changes": [{"valve_id": "V3", "action": "close"}]}' > /dev/null
curl -sS -o /tmp/resp.json -w "HTTP %{http_code}\n" -X POST "$BASE/api/derating/preview" \
  -H 'Content-Type: application/json' \
  -d "{\"revision\": $REV, \"sources\": [{\"facility_id\": \"S2\", \"capacity\": 3}]}"
pp < /tmp/resp.json
# 恢复阀位
curl -sS -X POST "$BASE/api/valves/change" \
  -H 'Content-Type: application/json' \
  -d '{"changes": [{"valve_id": "V3", "action": "open"}]}' > /dev/null
echo "(V3 已恢复开启)"

echo
echo "== 9. 确认预演不写入：管网额定容量与修订号均未被本轮预演改变 =="
curl -sS "$BASE/api/network" | pp
