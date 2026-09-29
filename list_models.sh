#!/usr/bin/env bash
set -euo pipefail

: "${TAMUS_AI_CHAT_API_KEY:?Set TAMUS_AI_CHAT_API_KEY first}"
export TAMUS_AI_CHAT_API_ENDPOINT="${TAMUS_AI_CHAT_API_ENDPOINT:-https://chat-api.tamu.ai}"

curl -s -X GET "${TAMUS_AI_CHAT_API_ENDPOINT}/api/models" \
  -H "Authorization: Bearer ${TAMUS_AI_CHAT_API_KEY}" \
| jq -r '(.data // .)[] | if type == "object" then (.id // .name // .model) else . end'
