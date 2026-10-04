#!/usr/bin/env bash
# 针对运行中的服务（默认 http://localhost:8000）演示完整业务流程。
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8000}"
say() { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }

say "健康检查"
curl -s "$BASE/health"; echo

say "判定示例：demo-derived-1 在 2026-03-01 可用于 research（交集仅 research）"
curl -s "$BASE/permissions/check?version_id=demo-derived-1&use=research&at=2026-03-01T00:00:00Z"; echo

say "判定示例：2026-07-01 申请 review（被 demo-root-a 阻断）"
curl -s "$BASE/permissions/check?version_id=demo-derived-1&use=review&at=2026-07-01T00:00:00Z"; echo

say "登记新派生版本（父版本 demo-root-a）"
curl -s -X POST "$BASE/versions" -H 'Content-Type: application/json' \
  -d '{"version_id":"example-v1","parent_ids":["demo-root-a"],"note":"调用示例"}'; echo

say "提交许可变更"
curl -s -X POST "$BASE/license-changes" -H 'Content-Type: application/json' \
  -d '{"change_id":"example-c1","version_id":"example-v1","effective_at":"2026-03-01T00:00:00Z","allowed_uses":["research"]}'; echo

say "重复提交：同标识同内容 -> 200 replayed=true"
curl -s -X POST "$BASE/license-changes" -H 'Content-Type: application/json' \
  -d '{"change_id":"example-c1","version_id":"example-v1","effective_at":"2026-03-01T00:00:00Z","allowed_uses":["research","research"]}'; echo

say "冲突 1：同标识不同内容 -> 409"
curl -s -X POST "$BASE/license-changes" -H 'Content-Type: application/json' \
  -d '{"change_id":"example-c1","version_id":"example-v1","effective_at":"2026-03-01T00:00:00Z","allowed_uses":["commercial"]}'; echo

say "冲突 2：同版本同生效时刻不同变更 -> 409"
curl -s -X POST "$BASE/license-changes" -H 'Content-Type: application/json' \
  -d '{"change_id":"example-c2","version_id":"example-v1","effective_at":"2026-03-01T00:00:00Z","allowed_uses":["research"]}'; echo

say "无有效许可（变更生效之前）-> allowed=false"
curl -s "$BASE/permissions/check?version_id=example-v1&use=research&at=2026-02-01T00:00:00Z"; echo
