#!/usr/bin/env bash
set -euo pipefail

for unit in sro-robot-infra.service sro-robot-web.service sro-robot-ocr@1.service sro-robot-preview.service nginx.service; do
    systemctl is-active --quiet "$unit"
done

for container in deploy-db-1 deploy-redis-1; do
    health="$(docker inspect --format '{{.State.Health.Status}}' "$container")"
    [[ "$health" == healthy ]]
done

curl --fail --silent --show-error --max-time 10 \
    --output /dev/null http://127.0.0.1:8000/readyz
