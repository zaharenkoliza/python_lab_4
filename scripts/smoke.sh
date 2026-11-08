#!/usr/bin/env bash
# Smoke-тест: все сервисы поднимаются, SUT отвечает всеми драйверами, метрики собираются.
set -euo pipefail
cd "$(dirname "$0")/.."
DC="docker compose"; $DC version >/dev/null 2>&1 || DC="docker-compose"
fail() { echo "SMOKE FAIL: $*"; exit 1; }
$DC --profile monitoring up -d --build --wait db prometheus cadvisor grafana
for d in sync_psycopg2 async_asyncpg async_psycopg3; do
  DRIVER=$d $DC up -d --force-recreate --wait sut
  curl -fsS localhost:8000/health | grep -q "$d" || fail "health $d"
  [ "$(curl -fsS 'localhost:8000/rows?start=5' | python3 -c 'import json,sys;print(len(json.load(sys.stdin)))')" = 100 ] || fail "rows $d"
  curl -fsS localhost:8000/slow | grep -q true || fail "slow $d"
  curl -fsS localhost:8000/metrics | grep -q http_requests_total || fail "metrics $d"
  echo "ok: $d"
done
sleep 12
up=$(curl -fsS localhost:9090/api/v1/targets | python3 -c 'import json,sys;print(sum(t["health"]=="up" for t in json.load(sys.stdin)["data"]["activeTargets"]))')
echo "prometheus targets up: $up"
[ "$up" -ge 1 ] || fail "prometheus не видит ни одной цели"
curl -fsS localhost:3000/api/health | grep -q ok || fail "grafana"
$DC --profile monitoring down -v
echo "SMOKE OK"
