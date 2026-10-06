#!/usr/bin/env bash
# 物业复水计划 —— 逐步预演 / 保存不可变计划 / 带修订号逐步执行 调用示例。
# 用法：bash examples/restore_demo.sh [BASE_URL]
set -euo pipefail

BASE="${1:-http://localhost:8080}"
JQ="$(command -v jq || true)"
pp() { if [ -n "$JQ" ]; then jq .; else cat; fi; }

echo "== 0. 健康检查 + 录入示例管网 =="
curl -sS "$BASE/health"; echo
curl -sS -X PUT "$BASE/api/network" \
  -H 'Content-Type: application/json' \
  --data @"$(dirname "$0")/sample_network.json" > /tmp/r_base.json
REV=$(python3 -c "import json;print(json.load(open('/tmp/r_base.json'))['revision'])")
echo "录入完成，基准修订号 revision=$REV"

echo
echo "== 1. 模拟抢修隔离：关闭 V1、V3（阀门变更会推进修订号） =="
curl -sS -X POST "$BASE/api/valves/change" \
  -H 'Content-Type: application/json' \
  -d '{"changes":[{"valve_id":"V1","action":"close"},{"valve_id":"V3","action":"close"}]}' \
  > /tmp/r_iso.json
REV=$(python3 -c "import json;print(json.load(open('/tmp/r_iso.json'))['revision'])")
echo "当前修订号 revision=$REV"

echo
echo "== 2. 非法步骤预演（未知阀门 + 非法动作 + 重复阀门 → 400 定位，计划不保存） =="
curl -sS -o /tmp/r_bad.json -w "HTTP %{http_code}\n" -X POST "$BASE/api/restore/plans" \
  -H 'Content-Type: application/json' \
  -d '{"steps":[
        {"valve_id":"V404","action":"open"},
        {"valve_id":"V3","action":"detonate"},
        {"valve_id":"V3","action":"open"}]}'
pp < /tmp/r_bad.json

echo
echo "== 3. 首个冲突预演（第 2 步开 V5 会连通互锁水源 S1/S2 → 409，不保存不改状态） =="
curl -sS -o /tmp/r_conf.json -w "HTTP %{http_code}\n" -X POST "$BASE/api/restore/plans" \
  -H 'Content-Type: application/json' \
  -d '{"steps":[
        {"valve_id":"V3","action":"open"},
        {"valve_id":"V5","action":"open"}]}'
python3 - <<'PY'
import json
d = json.load(open('/tmp/r_conf.json'))["error"]["details"][0]
print("冲突步骤位置:", d["position"], "| 连通水源:", d["source_a"], d["source_b"],
      "| 路径:", " -> ".join(d["path"]))
PY

echo
echo "== 4. 安全步骤全部通过：保存不可变计划（返回每步阀位 + 供水/欠供 + 基准修订号） =="
curl -sS -X POST "$BASE/api/restore/plans" \
  -H 'Content-Type: application/json' \
  -d '{"steps":[
        {"valve_id":"V3","action":"open"},
        {"valve_id":"V1","action":"open"}]}' > /tmp/r_plan.json
PID=$(python3 -c "import json;d=json.load(open('/tmp/r_plan.json'));print(d['plan_id'])")
python3 - <<PY
import json
d=json.load(open('/tmp/r_plan.json'))
print("plan_id =", d["plan_id"], "| base_revision =", d["base_revision"],
      "| total_steps =", d["total_steps"])
for s in d["steps"]:
    print(f"  第{s['position']}步 {s['action']} {s['valve_id']}："
          f"D1 供{[a['supplied'] for a in s['allocations'] if a['demand_id']=='D1'][0]}, "
          f"D2 供{[a['supplied'] for a in s['allocations'] if a['demand_id']=='D2'][0]}")
PY

echo
echo "== 5. 过期修订号执行（被拒 409，状态不变） =="
curl -sS -o /tmp/r_stale.json -w "HTTP %{http_code}\n" -X POST \
  "$BASE/api/restore/plans/$PID/execute" \
  -H 'Content-Type: application/json' -d "{\"revision\":$((REV-1))}"
python3 -c "import json;print(json.load(open('/tmp/r_stale.json'))['error']['message'])"

echo
echo "== 6. 用所见修订号执行第 1 步（同事务复核→更新阀位/进度→revision+1） =="
curl -sS -X POST "$BASE/api/restore/plans/$PID/execute" \
  -H 'Content-Type: application/json' -d "{\"revision\":$REV}" > /tmp/r_ex1.json
REV=$(python3 -c "import json;print(json.load(open('/tmp/r_ex1.json'))['revision'])")
python3 -c "import json;d=json.load(open('/tmp/r_ex1.json'));print('已执行第',d['position'],'步，新修订号',d['revision'],'下一步',d['next_step'])"

echo
echo "== 7. 重复提交已完成的第 1 步（幂等回放原结果，修订号不变） =="
curl -sS -X POST "$BASE/api/restore/plans/$PID/execute" \
  -H 'Content-Type: application/json' -d "{\"revision\":$REV,\"step\":1}" \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print('idempotent_replay =',d['idempotent_replay'],'| revision =',d['revision'])"

echo
echo "== 8. 跳步执行第 5 步（被拒 409，状态不变） =="
curl -sS -o /tmp/r_jump.json -w "HTTP %{http_code}\n" -X POST \
  "$BASE/api/restore/plans/$PID/execute" \
  -H 'Content-Type: application/json' -d "{\"revision\":$REV,\"step\":5}"
python3 -c "import json;print(json.load(open('/tmp/r_jump.json'))['error']['message'])"

echo
echo "== 9. 执行下一步（第 2 步），计划完成 =="
curl -sS -X POST "$BASE/api/restore/plans/$PID/execute" \
  -H 'Content-Type: application/json' -d "{\"revision\":$REV}" \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print('status =',d['status'],'| revision =',d['revision'],'| next_step =',d['next_step'])"

echo
echo "== 10. 计划详情与列表（GET） =="
curl -sS "$BASE/api/restore/plans/$PID" | pp
curl -sS "$BASE/api/restore/plans" | python3 -c "
import sys,json
for p in json.load(sys.stdin)['plans']:
    print(p['plan_id'], p['status'], f\"{p['next_step']}/{p['total_steps']}\")"

echo
echo "复水演示完成。"
