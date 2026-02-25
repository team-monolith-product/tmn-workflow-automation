#!/bin/bash
set -e

# Claude OAuth
if [ -n "$CLAUDE_CREDS" ]; then
    echo "$CLAUDE_CREDS" > /home/agent/.claude/.credentials.json
fi

# gh CLI
if [ -n "$GITHUB_TOKEN" ]; then
    echo "$GITHUB_TOKEN" | gh auth login --with-token 2>/dev/null || true
fi

# aha-memory clone
MEMORY_REPO="https://${GITHUB_TOKEN}@github.com/team-monolith-product/aha-memory.git"
MEMORY_PATH="/home/agent/aha-memory"
for i in 1 2 3; do
    git clone "$MEMORY_REPO" "$MEMORY_PATH" 2>/dev/null && break
    echo "[aha] git clone attempt $i failed, retrying..." >&2
    sleep 2
done

# git user config (memory MCP commit용)
git -C "$MEMORY_PATH" config user.name "aha"
git -C "$MEMORY_PATH" config user.email "aha@team-monolith.com"

# 실행 맥락 구성
TRIGGER="${AHA_TRIGGER:-manual}"
CHANNEL="${AHA_CHANNEL}"
THREAD_TS="${AHA_THREAD_TS}"
REPORT_CHANNEL="${AHA_REPORT_CHANNEL:-C0AHU2XDBT2}"

CONTEXT=""
case "$TRIGGER" in
    cron)
        CONTEXT="You were triggered by a scheduled cron job.
Report your results to Slack channel ${REPORT_CHANNEL} when done."
        ;;
    slack)
        CONTEXT="You were triggered by a Slack mention.
Reply to the thread: channel=${CHANNEL}, thread_ts=${THREAD_TS}."
        ;;
    *)
        CONTEXT="You were triggered manually.
Report your results to Slack channel ${REPORT_CHANNEL} when done."
        ;;
esac

# system prompt 조립
APPEND="
## Trigger Context

${CONTEXT}"

if [ -s "$MEMORY_PATH/MEMORY.md" ]; then
    APPEND="${APPEND}

## Procedural Memory (from /home/agent/aha-memory/MEMORY.md)

$(cat "$MEMORY_PATH/MEMORY.md")"
fi

# MCP 설정
MCP_CONFIG='{
  "mcpServers": {
    "notion": { "command": "notion-mcp-server", "args": [], "env": {} },
    "slack": { "command": "mcp-server-slack", "args": [], "env": {} },
    "memory": { "command": "node", "args": ["/opt/mcp-memory/dist/index.js"], "env": { "MEMORY_REPO_PATH": "/home/agent/aha-memory" } }
  }
}'

# permissions
cat > /home/agent/.claude/settings.json << 'SETTINGS'
{
  "permissions": {
    "allow": [
      "mcp__notion__API-post-search",
      "mcp__notion__API-retrieve-a-page",
      "mcp__notion__API-patch-page",
      "mcp__notion__API-post-page",
      "mcp__notion__API-retrieve-a-page-property",
      "mcp__notion__API-query-data-source",
      "mcp__notion__API-retrieve-a-data-source",
      "mcp__notion__API-retrieve-a-database",
      "mcp__notion__API-get-block-children",
      "mcp__notion__API-patch-block-children",
      "mcp__notion__API-retrieve-a-block",
      "mcp__notion__API-update-a-block",
      "mcp__slack__slack_get_channel_history",
      "mcp__slack__slack_get_thread_replies",
      "mcp__slack__slack_list_channels",
      "mcp__slack__slack_post_message",
      "mcp__slack__slack_reply_to_thread",
      "mcp__slack__slack_add_reaction",
      "mcp__memory__memory_update"
    ],
    "deny": [
      "mcp__notion__API-get-user",
      "mcp__notion__API-get-users",
      "mcp__notion__API-get-self",
      "mcp__notion__API-delete-a-block",
      "mcp__notion__API-create-a-comment",
      "mcp__notion__API-retrieve-a-comment",
      "mcp__notion__API-update-a-data-source",
      "mcp__notion__API-create-a-data-source",
      "mcp__notion__API-list-data-source-templates",
      "mcp__notion__API-move-page",
      "mcp__slack__slack_get_users",
      "mcp__slack__slack_get_user_profile"
    ],
    "defaultMode": "bypassPermissions"
  }
}
SETTINGS

TASK="${1:-시스템 상태를 점검하고 이상 유무를 보고하세요.}"

claude -p "$TASK" \
    --dangerously-skip-permissions \
    --mcp-config "$MCP_CONFIG" \
    --append-system-prompt "$APPEND" \
    --output-format json \
    || EXIT_CODE=$?

# 세션 로그를 /sessions 로 복사 (호스트 마운트용)
if [ -d /sessions ]; then
    cp /home/agent/.claude/projects/*/*.jsonl /sessions/ 2>/dev/null || true
    chmod a+r /sessions/*.jsonl 2>/dev/null || true
fi

exit ${EXIT_CODE:-0}
