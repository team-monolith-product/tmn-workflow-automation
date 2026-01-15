#!/usr/bin/env python3
"""
Find incomplete requests in Rails logs
Fetches logs from CloudWatch and analyzes incomplete requests before OOM
"""
import json
import re
import sys
import argparse
from collections import defaultdict
from datetime import datetime, timedelta
import boto3


def get_stream_last_timestamp(client, log_group, stream_name):
    """Get the last event timestamp from the log stream"""
    try:
        response = client.describe_log_streams(
            logGroupName=log_group, logStreamNamePrefix=stream_name, limit=1
        )
        streams = response.get("logStreams", [])
        for stream in streams:
            if stream["logStreamName"] == stream_name:
                return stream.get("lastEventTimestamp")
        return None
    except Exception as e:
        print(f"Error getting stream info: {e}", file=sys.stderr)
        return None


def fetch_logs_from_cloudwatch(
    client, log_group, log_stream, start_time=None, end_time=None
):
    """Fetch logs from CloudWatch"""
    logs = []

    kwargs = {"logGroupName": log_group, "logStreamNames": [log_stream]}

    if start_time:
        kwargs["startTime"] = start_time
    if end_time:
        kwargs["endTime"] = end_time

    try:
        paginator = client.get_paginator("filter_log_events")

        for page in paginator.paginate(**kwargs):
            for event in page["events"]:
                logs.append(
                    {"message": event["message"], "timestamp": event["timestamp"]}
                )

        return logs
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return []


def parse_log_message(line):
    """Parse Rails log message from CloudWatch JSON format"""
    try:
        data = json.loads(line)
        log_text = data.get("log", "")

        # Extract timestamp
        timestamp_match = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", log_text)
        timestamp = timestamp_match.group(1) if timestamp_match else None

        # Extract the actual Rails log
        match = re.search(r"INFO -- : (.*)", log_text)
        if match:
            return {"log": match.group(1), "timestamp": timestamp}
        return None
    except:
        return None


def extract_request_id(log_line):
    """Extract request ID from Rails log line"""
    match = re.search(r"\[([a-f0-9\-]{36})\]", log_line)
    return match.group(1) if match else None


def is_started_request(log_line):
    """Check if log line is a Started request"""
    return "Started " in log_line and any(
        method in log_line
        for method in [" GET ", " POST ", " PUT ", " PATCH ", " DELETE "]
    )


def is_completed_request(log_line):
    """Check if log line is a Completed request"""
    return "Completed " in log_line and re.search(r"Completed \d+", log_line)


def extract_request_info(log_line):
    """Extract request information from Started log line"""
    match = re.search(
        r'Started (GET|POST|PUT|PATCH|DELETE) \\?"([^"\\]+)\\?" for ([\d.]+)', log_line
    )
    if match:
        return {"method": match.group(1), "path": match.group(2), "ip": match.group(3)}
    return None


def should_exclude_path(path):
    """Check if path should be excluded from analysis"""
    excluded = ["/health_check", "/metrics", "/healthz", "/readiness", "/liveness"]
    return path in excluded


def main():
    parser = argparse.ArgumentParser(description="Find incomplete requests before OOM")
    parser.add_argument("--log-stream", required=True, help="Log stream name")
    # Log group is hardcoded to /aws/containerinsights/ped-eks-cluster-v2-service-all-prd/application
    parser.add_argument(
        "--minutes-before",
        type=int,
        default=5,
        help="Minutes before OOM to analyze (default: 5)",
    )
    parser.add_argument("--profile", help="AWS profile name")
    parser.add_argument("--region", default="ap-northeast-2", help="AWS region")

    args = parser.parse_args()

    # Create CloudWatch client
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    client = session.client("logs")
    log_group = "/aws/containerinsights/ped-eks-cluster-v2-service-all-prd/application"

    # Get stream last timestamp
    last_timestamp = get_stream_last_timestamp(client, log_group, args.log_stream)
    if not last_timestamp:
        print(
            f"Error: Could not find stream {args.log_stream} or it has no events",
            file=sys.stderr,
        )
        return 1

    end_time = datetime.fromtimestamp(last_timestamp / 1000)
    start_time = end_time - timedelta(minutes=args.minutes_before)

    print(f"Stream Last Event: {end_time.isoformat()}")
    print(f"Analysis window: {start_time.isoformat()} to {end_time.isoformat()}")
    print(f"Fetching logs from CloudWatch...")
    print()

    # Fetch logs
    start_time_ms = int(start_time.timestamp() * 1000)
    end_time_ms = int(end_time.timestamp() * 1000)

    raw_logs = fetch_logs_from_cloudwatch(
        client, log_group, args.log_stream, start_time_ms, end_time_ms
    )

    if not raw_logs:
        print("No logs found", file=sys.stderr)
        return 1

    print(f"Fetched {len(raw_logs)} total log lines")

    # Filter logs by time range (already done by CloudWatch, but keeping for safety)
    filtered_logs = [
        log for log in raw_logs if start_time_ms <= log["timestamp"] <= end_time_ms
    ]
    print(f"Filtered to {len(filtered_logs)} log lines in time range")
    print()

    # Track request lifecycle
    started_requests = {}
    completed_requests = set()

    # Process logs
    for log_entry in filtered_logs:
        parsed = parse_log_message(log_entry["message"])
        if not parsed:
            continue

        log_line = parsed["log"]
        timestamp = parsed["timestamp"]

        request_id = extract_request_id(log_line)
        if not request_id:
            continue

        if is_started_request(log_line):
            request_info = extract_request_info(log_line)
            if request_info:
                started_requests[request_id] = {
                    "info": request_info,
                    "timestamp": timestamp,
                    "log_line": log_line,
                }
        elif is_completed_request(log_line):
            completed_requests.add(request_id)

    # Find incomplete requests
    incomplete_requests = []
    for request_id, request_data in started_requests.items():
        if request_id not in completed_requests:
            path = request_data["info"]["path"]

            if should_exclude_path(path):
                continue

            incomplete_requests.append(
                {
                    "request_id": request_id,
                    "method": request_data["info"]["method"],
                    "path": path,
                    "ip": request_data["info"]["ip"],
                    "timestamp": request_data["timestamp"],
                    "log_line": request_data["log_line"],
                }
            )

    # Sort by timestamp
    incomplete_requests.sort(key=lambda x: x["timestamp"] or "")

    print("=" * 80)
    print("ANALYSIS RESULTS")
    print("=" * 80)
    print(f"Requests started: {len(started_requests)}")
    print(f"Requests completed: {len(completed_requests)}")
    print(f"Incomplete requests: {len(incomplete_requests)}")
    print()

    if incomplete_requests:
        print("=" * 80)
        print("INCOMPLETE REQUESTS (requests that started but never completed)")
        print("=" * 80)

        # Group by path
        by_path = defaultdict(list)
        for req in incomplete_requests:
            by_path[req["path"]].append(req)

        for path, reqs in sorted(
            by_path.items(), key=lambda x: len(x[1]), reverse=True
        ):
            print(f"\n[{len(reqs)}x] {reqs[0]['method']} {path}")
            unique_ips = list(set(req["ip"] for req in reqs))
            print(f"  IPs: {', '.join(unique_ips[:5])}")
            if len(unique_ips) > 5:
                print(f"       ... and {len(unique_ips) - 5} more IPs")
            print(f"  Request IDs:")
            for req in reqs[:3]:
                print(f"    - {req['request_id']} at {req['timestamp']}")
            if len(reqs) > 3:
                print(f"    ... and {len(reqs) - 3} more")
    else:
        print("No incomplete requests found.")
        print("\nThis suggests:")
        print("  - All requests completed normally before OOM")
        print("  - OOM likely caused by memory accumulation across many requests")
        print("  - Consider analyzing request patterns and response sizes")

    return 0


if __name__ == "__main__":
    sys.exit(main())
