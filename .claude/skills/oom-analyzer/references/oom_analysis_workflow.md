# OOM Analysis Workflow

Complete workflow for analyzing OOM (Out of Memory) incidents in Kubernetes-based Rails applications.

## Overview

This workflow helps identify the root cause of container restarts by:
1. Confirming the restart was actually OOM-related
2. Identifying suspicious requests before the restart
3. Analyzing request payload sizes
4. Generating hypotheses about the root cause

## Step-by-Step Workflow

### 1. Receive Grafana Alert

When you receive a "Container Restarts" alert from Grafana:
- Note the pod name (e.g., `class-rails-service-apne2-prd-d795f9b9-gn6pj`)
- Note the namespace (e.g., `class-rails`)
- Note the restart time
- Check Grafana dashboard URL from the alert

### 2. Verify OOM Kill

**Goal**: Confirm if the restart was caused by OOMKilled.

**Methods**:
a. Check current/recent pods:
```bash
kubectl get pods -n <namespace>
kubectl describe pod <pod-name> -n <namespace> | grep -A 20 "Last State"
```

Look for:
```
Last State:     Terminated
  Reason:       OOMKilled
  Exit Code:    137
```

b. Check Kubernetes events (if pod still exists):
```bash
kubectl get events -n <namespace> | grep <pod-name>
```

**Note**: If the pod was replaced by a new deployment, events may be expired. In this case, proceed with log analysis.

### 3. Fetch CloudWatch Logs

**Goal**: Get application logs from the minutes before the restart.

Use `fetch_cloudwatch_logs.py`:
```bash
python scripts/fetch_cloudwatch_logs.py \
  --pod-name <pod-name> \
  --namespace <namespace> \
  --log-group /aws/containerinsights/<cluster>/application \
  --oom-time "YYYY-MM-DDTHH:MM:SS" \
  --minutes-before 10 \
  --output logs.json
```

**Key observations**:
- Last log timestamp (pod died immediately after)
- Any error messages before termination
- Request patterns in final minutes

### 4. Find Incomplete Requests

**Goal**: Identify requests that started but never completed.

Use `find_incomplete_requests.py`:
```bash
python scripts/find_incomplete_requests.py \
  --input logs.json \
  --last-n-minutes 5 \
  --output incomplete.json
```

This script:
- Matches "Started" with "Completed" log lines by Request ID
- Excludes monitoring endpoints (/health_check, /metrics)
- Shows which endpoints had incomplete requests

**Analysis**:
- High count of incomplete requests for a specific endpoint → suspicious
- Multiple incomplete requests from same IPs → potential attack or bug
- Timing pattern (e.g., every 15 seconds) → automated process

### 5. Query ALB Access Logs

**Goal**: Determine actual payload sizes for suspicious requests.

Use `query_alb_access_logs.py`:
```bash
python scripts/query_alb_access_logs.py \
  --incomplete-requests incomplete.json \
  --database default \
  --table <alb-logs-table> \
  --output-location s3://<bucket>/athena-results/ \
  --output alb_analysis.json
```

Or manually query Athena:
```sql
SELECT
    time,
    request_url,
    received_bytes,
    sent_bytes,
    target_processing_time,
    client_ip
FROM <table>
WHERE day = 'YYYY/MM/DD'
    AND time >= 'YYYY-MM-DDTHH:MM:SS'
    AND time <= 'YYYY-MM-DDTHH:MM:SS'
    AND request_url LIKE '%<path>%'
ORDER BY CAST(received_bytes AS INTEGER) DESC
LIMIT 50
```

**Key metrics**:
- `received_bytes`: Request payload size
- `sent_bytes`: Response payload size
- Large responses (>10KB) can accumulate memory
- Many concurrent large requests → OOM

### 6. Generate Root Cause Hypotheses

Based on the collected evidence, formulate hypotheses:

**Common OOM patterns**:

1. **Large Response Accumulation**
   - Evidence: Many requests with large `sent_bytes` (10-20KB+)
   - Response size >> request size (5-10x)
   - Multiple concurrent requests
   - Hypothesis: Application generates large responses that accumulate in memory

2. **Memory Leak in Request Handler**
   - Evidence: Incomplete requests for specific endpoint
   - Requests never complete (no "Completed" log)
   - Memory not released after request
   - Hypothesis: Request handler has memory leak

3. **Payload Too Large**
   - Evidence: Requests with `received_bytes` > 1MB
   - Few requests but very large
   - Hypothesis: Large input payloads overwhelming memory

4. **Concurrent Request Spike**
   - Evidence: Sudden spike in request count
   - Many simultaneous incomplete requests
   - Hypothesis: Traffic spike exceeding memory limits

## Output Format

Present findings as:

### Evidence Summary
- Container restart time: [timestamp]
- Pod: [name]
- Confirmed OOM: [Yes/No]

### Suspicious Requests
- Endpoint: [path]
- Count: [number]
- Time range: [start - end]
- Request payload: [size range]
- Response payload: [size range]

### Root Cause Hypotheses
1. [Hypothesis with supporting evidence]
2. [Alternative hypothesis]

### Recommended Actions
- [Immediate actions]
- [Investigation steps]
- [Long-term fixes]
