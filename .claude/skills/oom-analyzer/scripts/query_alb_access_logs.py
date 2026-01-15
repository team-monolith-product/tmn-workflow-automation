#!/usr/bin/env python3
"""
Query ALB Access Logs from Athena to find payload sizes for suspicious requests
"""
import boto3
import json
import sys
import time
import argparse
from datetime import datetime, timedelta


# Configuration
ATHENA_DATABASE = "default"
ATHENA_TABLE = "ped_alb_access_logs_prd"
ATHENA_OUTPUT_LOCATION = "s3://tmn-bucket-athena-result/"
AWS_REGION = "ap-northeast-2"


def create_athena_client(profile=None, region=AWS_REGION):
    """Create Athena client"""
    session = boto3.Session(profile_name=profile, region_name=region)
    return session.client("athena")


def execute_athena_query(
    client, query, database=ATHENA_DATABASE, output_location=ATHENA_OUTPUT_LOCATION
):
    """Execute Athena query and wait for results"""
    print(f"Executing query...")
    print(f"Query: {query[:200]}..." if len(query) > 200 else f"Query: {query}")
    print()

    response = client.start_query_execution(
        QueryString=query,
        QueryExecutionContext={"Database": database},
        ResultConfiguration={"OutputLocation": output_location},
    )

    query_execution_id = response["QueryExecutionId"]
    print(f"Query execution ID: {query_execution_id}")

    # Wait for query to complete
    max_execution_time = 120  # seconds
    start_time = time.time()

    while True:
        response = client.get_query_execution(QueryExecutionId=query_execution_id)
        status = response["QueryExecution"]["Status"]["State"]

        if status in ["SUCCEEDED", "FAILED", "CANCELLED"]:
            break

        if time.time() - start_time > max_execution_time:
            print("Query timeout!", file=sys.stderr)
            return None

        print(f"Status: {status}... waiting")
        time.sleep(2)

    if status != "SUCCEEDED":
        reason = response["QueryExecution"]["Status"].get(
            "StateChangeReason", "Unknown"
        )
        print(f"Query failed: {reason}", file=sys.stderr)
        return None

    print(f"Query completed successfully!")
    return query_execution_id


def get_query_results(client, query_execution_id):
    """Fetch query results"""
    results = []
    paginator = client.get_paginator("get_query_results")

    for page in paginator.paginate(QueryExecutionId=query_execution_id):
        # Skip header row
        rows = page["ResultSet"]["Rows"]
        if not results:
            # First page includes header
            rows = rows[1:]

        for row in rows:
            values = [col.get("VarCharValue", "") for col in row["Data"]]
            results.append(values)

    return results


def build_alb_query(path, start_time, end_time, table_name=ATHENA_TABLE):
    """Build Athena query for ALB access logs"""
    # ALB logs are partitioned by day in format YYYY/MM/DD
    start_dt = datetime.fromisoformat(start_time)
    end_dt = datetime.fromisoformat(end_time)

    # Generate list of days to query
    days = set()
    current = start_dt.date()
    while current <= end_dt.date():
        days.add(current.strftime("%Y/%m/%d"))
        current = current + timedelta(days=1)

    day_conditions = " OR ".join([f"day = '{day}'" for day in sorted(days)])

    query = f"""
    SELECT
        time,
        request_url,
        received_bytes,
        sent_bytes,
        target_status_code,
        request_processing_time,
        target_processing_time,
        response_processing_time,
        elb_status_code,
        client_ip,
        request_verb,
        request_creation_time
    FROM {table_name}
    WHERE ({day_conditions})
        AND time >= '{start_time}'
        AND time <= '{end_time}'
        AND request_url LIKE '%{path}%'
    ORDER BY CAST(sent_bytes AS INTEGER) DESC
    LIMIT 50
    """

    return query


def main():
    parser = argparse.ArgumentParser(
        description="Query ALB Access Logs for payload sizes"
    )
    parser.add_argument(
        "--path", required=True, help="Request path to analyze (e.g., /api/v1/heavy)"
    )
    parser.add_argument(
        "--oom-time",
        required=True,
        help="OOM time in ISO format (e.g., 2025-11-21T17:35:16)",
    )
    parser.add_argument(
        "--minutes-before",
        type=int,
        default=10,
        help="Minutes before OOM to analyze (default: 10)",
    )
    parser.add_argument("--profile", help="AWS profile name")
    parser.add_argument("--region", default=AWS_REGION, help="AWS region")
    parser.add_argument("--output", help="Output JSON file")

    args = parser.parse_args()

    # Calculate time range
    oom_time = datetime.fromisoformat(args.oom_time)
    start_time = oom_time - timedelta(minutes=args.minutes_before)
    end_time = oom_time + timedelta(minutes=1)  # Add 1 minute buffer after OOM

    print(f"Analyzing path: {args.path}")
    print(f"Time range: {start_time.isoformat()} to {end_time.isoformat()}")
    print()

    # Create Athena client
    client = create_athena_client(args.profile, args.region)

    # Build and execute query
    query = build_alb_query(args.path, start_time.isoformat(), end_time.isoformat())

    query_execution_id = execute_athena_query(client, query)

    if not query_execution_id:
        print(f"Failed to execute query", file=sys.stderr)
        return 1

    # Get results
    results = get_query_results(client, query_execution_id)
    print(f"\nFound {len(results)} matching ALB log entries")
    print()

    # Parse results
    parsed_results = []
    for row in results:
        if len(row) >= 12:
            parsed_results.append(
                {
                    "time": row[0],
                    "request_url": row[1],
                    "received_bytes": row[2],
                    "sent_bytes": row[3],
                    "target_status_code": row[4],
                    "request_processing_time": row[5],
                    "target_processing_time": row[6],
                    "response_processing_time": row[7],
                    "elb_status_code": row[8],
                    "client_ip": row[9],
                    "request_verb": row[10],
                    "request_creation_time": row[11],
                }
            )

    # Print summary
    if parsed_results:
        print("Payload size analysis (Top 50 by response size):")

        received_sizes = [
            int(r["received_bytes"])
            for r in parsed_results
            if r["received_bytes"].isdigit()
        ]
        sent_sizes = [
            int(r["sent_bytes"]) for r in parsed_results if r["sent_bytes"].isdigit()
        ]

        if received_sizes:
            print(f"  Received bytes (Request Payload):")
            print(f"    Max: {max(received_sizes):,} bytes")
            print(f"    Avg: {sum(received_sizes) // len(received_sizes):,} bytes")

        if sent_sizes:
            print(f"  Sent bytes (Response Payload):")
            print(f"    Max: {max(sent_sizes):,} bytes")
            print(f"    Avg: {sum(sent_sizes) // len(sent_sizes):,} bytes")

        print("\n  Top 5 largest responses:")
        for r in parsed_results[:5]:
            print(
                f"    {r['time']}: {r['sent_bytes']:>10} bytes sent, "
                f"{r['received_bytes']:>10} bytes received, status: {r['target_status_code']}"
            )
    else:
        print("No logs found for this path in the specified time range.")

    # Save results
    output_data = {
        "query_time": datetime.now().isoformat(),
        "path": args.path,
        "time_range": {"start": start_time.isoformat(), "end": end_time.isoformat()},
        "results": parsed_results,
    }

    if args.output:
        with open(args.output, "w") as f:
            json.dump(output_data, f, indent=2)
        print(f"\nResults saved to: {args.output}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
