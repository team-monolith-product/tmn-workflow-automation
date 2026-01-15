---
name: rails-pod-restart-request-searcher
description: Analyze Kubernetes rails container restarts to search incomplete requests. Use when you receive Grafana "Container Restarts" alerts for rails applications.
---

## Analysis Workflow

### Step 1: Extract Alert Information

From the Grafana Slack alert, extract:
- **Pod name**: e.g., `class-rails-service-apne2-prd-d795f9b9-gn6pj`
- **Timestamp**: When the alert fired

### Step 2: Find CloudWatch Log Stream

Use `list_log_streams.py` to find the log stream that ended just before the restart (OOM).

```bash
python scripts/list_log_streams.py \
  --pod-name <full-pod-name> \
  --region ap-northeast-2
```

**Selection Criteria**:
- Look for the stream whose `Last` time is closest to the OOM/restart time.
- If streams are split, the one ending before the restart contains the relevant logs.

### Step 3: Find Incomplete Requests

Use `find_incomplete_requests.py` to identify requests that started but never completed in the selected stream.

```bash
python scripts/find_incomplete_requests.py \
  --log-stream '<full-stream-name-from-step-2>' \
  --minutes-before 5
```

This script fetches logs from CloudWatch and shows which API endpoints had incomplete requests.

### Step 4: Analyze Payload Sizes

Use `query_alb_access_logs.py` to check request and response sizes via Athena for suspicious paths found in Step 3.

```bash
python scripts/query_alb_access_logs.py \
  --path '<suspicious-path>' \
  --oom-time 'YYYY-MM-DDTHH:MM:SS' \
  --minutes-before 10
```

### Step 5: Summarize Abnormal Findings

Summarize the abnormal characteristics (Timing, Payloads, Duration, Concurrency) to assist the developer.

## Output Format

### Evidence Summary
- **Pod**: [name]
- **Analysis window**: [time range]

### Suspicious Requests
- **Endpoint**: [path]
- **Incomplete request count**: [number]
- **Request payload**: [min-max bytes]
- **Response payload**: [min-max bytes]

### Abnormality Description
- Describe specifically what was abnormal about these requests.