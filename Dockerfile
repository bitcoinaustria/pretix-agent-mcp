FROM python:3.13-slim

# The audit log and the pending-approval database live in /state — mount it, or an
# approved-but-not-executed action disappears when the container restarts.
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY pretix_agent_mcp ./pretix_agent_mcp
RUN pip install --no-cache-dir . && mkdir -p /state

ENV MCP_HOST=0.0.0.0 \
    MCP_PORT=8765 \
    STATE_DB=/state/pending-actions.sqlite3 \
    AUDIT_LOG=/state/audit.jsonl
EXPOSE 8765

# Configuration comes from the environment; nothing secret is baked into the image.
# Publish the port to 127.0.0.1 only and put TLS in front of it.
ENTRYPOINT ["pretix-agent-mcp"]
CMD ["serve"]
