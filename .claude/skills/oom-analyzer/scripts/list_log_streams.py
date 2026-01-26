#!/usr/bin/env python3
"""
List CloudWatch log streams for a pod
Shows all matching streams for manual selection
"""

import boto3
import sys
from datetime import datetime
import argparse


def format_time(timestamp_ms):
    """Convert millisecond timestamp to readable format"""
    if not timestamp_ms:
        return "N/A"
    return datetime.fromtimestamp(timestamp_ms / 1000).strftime("%Y-%m-%d %H:%M:%S")


def list_streams(client, log_group, pod_name):
    """List log streams for a pod

    Args:
        client: boto3 CloudWatch Logs client
        log_group: CloudWatch log group name
        pod_name: Kubernetes pod name

    Returns:
        List of stream info dicts
    """
    streams = []

    try:
        paginator = client.get_paginator("describe_log_streams")

        for page in paginator.paginate(
            logGroupName=log_group, orderBy="LastEventTime", descending=True
        ):
            for stream in page["logStreams"]:
                stream_name = stream["logStreamName"]
                first_event = stream.get("firstEventTimestamp")
                last_event = stream.get("lastEventTimestamp")

                # Check if stream matches pod name
                if pod_name in stream_name:
                    streams.append(
                        {
                            "name": stream_name,
                            "first_event_time": first_event,
                            "last_event_time": last_event,
                        }
                    )

                    first_str = format_time(first_event)
                    last_str = format_time(last_event)

                    print(f"{stream_name}")
                    print(f"First: {first_str}  |  Last: {last_str}")
                    print()

        return streams

    except Exception as e:
        print(f"Error listing log streams: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        return []


def main():
    parser = argparse.ArgumentParser(
        description="List CloudWatch log streams for a pod"
    )
    parser.add_argument(
        "--pod-name",
        required=True,
        help="Pod name (e.g., class-rails-service-apne2-prd-756c9bf4ff-hz66f)",
    )
    parser.add_argument(
        "--region",
        default="ap-northeast-2",
        help="AWS region (default: ap-northeast-2)",
    )
    parser.add_argument("--profile", help="AWS profile name")

    args = parser.parse_args()

    # Create CloudWatch client
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    client = session.client("logs")

    # List streams
    log_group = "/aws/containerinsights/ped-eks-cluster-v2-service-all-prd/application"
    streams = list_streams(client, log_group, args.pod_name)

    return 0


if __name__ == "__main__":
    sys.exit(main())
