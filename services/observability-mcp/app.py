import gzip
import json
import os
import re
from datetime import datetime, timedelta, timezone

import boto3
import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()

mcp = FastMCP("observability")

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
s3 = boto3.client("s3", region_name=AWS_REGION)

# Matches keys like logs/2026/07/16/yolo_154239.gz-objectVMo8diiJ
_KEY_RE = re.compile(r"logs/\d{4}/\d{2}/\d{2}/([A-Za-z0-9_-]+?)_(\d{6})\.gz")


def _bucket(env: str) -> str:
    key = f"{env.upper()}_S3_LOGS_BUCKET"
    value = os.environ.get(key)
    if not value:
        raise ValueError(f"Missing environment variable {key} (env must be 'dev' or 'prod')")
    return value


def _prometheus_url(env: str) -> str:
    key = f"{env.upper()}_PROMETHEUS_URL"
    value = os.environ.get(key)
    if not value:
        raise ValueError(f"Missing environment variable {key} (env must be 'dev' or 'prod')")
    return value.rstrip("/")


def _date_prefixes(days_back: int = 1) -> list[str]:
    now = datetime.now(timezone.utc)
    return [f"logs/{(now - timedelta(days=i)):%Y/%m/%d}/" for i in range(days_back + 1)]


def _list_recent_objects(bucket: str, minutes: int) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    results = []
    paginator = s3.get_paginator("list_objects_v2")
    for prefix in _date_prefixes():
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                if obj["LastModified"] < cutoff:
                    continue
                match = _KEY_RE.search(obj["Key"])
                results.append({
                    "key": obj["Key"],
                    "service": match.group(1) if match else None,
                    "uploaded_at": obj["LastModified"].isoformat(),
                })
    return results


def _read_object_lines(bucket: str, key: str) -> list[str]:
    body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    return gzip.decompress(body).decode(errors="replace").splitlines()


def _format_docker_line(raw_line: str) -> str:
    try:
        record = json.loads(raw_line)
    except (json.JSONDecodeError, TypeError):
        return raw_line
    ts = record.get("time", "")
    msg = record.get("log", "").rstrip("\n")
    return f"{ts} {msg}"


@mcp.tool()
def list_log_sources(env: str = "dev", minutes: int = 60) -> list[str]:
    """List which services have shipped logs to S3 in the last N minutes."""
    objects = _list_recent_objects(_bucket(env), minutes)
    return sorted({o["service"] for o in objects if o["service"]})


@mcp.tool()
def get_container_logs(service: str, minutes: int = 5, env: str = "dev", max_lines: int = 200) -> str:
    """Return recent log lines for a service (e.g. 'yolo', 'agent', 'frontend',
    'img-proc-mcp'), shipped via Fluent Bit to S3 in the last N minutes."""
    bucket = _bucket(env)
    objects = [o for o in _list_recent_objects(bucket, minutes) if o["service"] == service]
    if not objects:
        return f"No log objects found for service='{service}' in the last {minutes} minute(s)."

    objects.sort(key=lambda o: o["uploaded_at"])
    lines: list[str] = []
    for obj in objects:
        lines.extend(_format_docker_line(l) for l in _read_object_lines(bucket, obj["key"]) if l.strip())

    return "\n".join(lines[-max_lines:]) if lines else "Log objects found but contained no lines."


@mcp.tool()
def query_prometheus(promql: str, env: str = "dev") -> dict:
    """Run an instant PromQL query against the environment's Prometheus server."""
    resp = httpx.get(f"{_prometheus_url(env)}/api/v1/query", params={"query": promql}, timeout=15.0)
    resp.raise_for_status()
    return resp.json()


@mcp.tool()
def query_prometheus_range(promql: str, minutes: int = 10, step_seconds: int = 15, env: str = "dev") -> dict:
    """Run a PromQL range query over the last N minutes against the environment's Prometheus."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=minutes)
    params = {"query": promql, "start": start.timestamp(), "end": end.timestamp(), "step": step_seconds}
    resp = httpx.get(f"{_prometheus_url(env)}/api/v1/query_range", params=params, timeout=15.0)
    resp.raise_for_status()
    return resp.json()


@mcp.tool()
def get_cpu_usage(minutes: int = 10, env: str = "dev") -> dict:
    """Get host CPU usage percentage (non-idle) over the last N minutes, via node-exporter metrics."""
    promql = '100 - (avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[2m])) * 100)'
    return query_prometheus_range(promql, minutes=minutes, env=env)


@mcp.tool()
def investigate_error_at_time(service: str, timestamp: str, window_minutes: int = 5, env: str = "dev") -> dict:
    """Investigate what happened to a service around a specific UTC timestamp
    (ISO format, e.g. '2026-07-01T12:00:00'). Correlates S3 logs and Prometheus
    error-rate metrics from window_minutes before to window_minutes after."""
    try:
        target = datetime.fromisoformat(timestamp).replace(tzinfo=timezone.utc)
    except ValueError:
        return {"error": f"Could not parse timestamp '{timestamp}'. Use ISO format, e.g. 2026-07-01T12:00:00"}

    now = datetime.now(timezone.utc)
    lookback_minutes = int((now - target).total_seconds() / 60) + window_minutes
    if lookback_minutes < 0:
        return {"error": "Timestamp is in the future."}

    bucket = _bucket(env)
    lower = target - timedelta(minutes=window_minutes)
    upper = target + timedelta(minutes=window_minutes)

    objects = [
        o for o in _list_recent_objects(bucket, lookback_minutes)
        if o["service"] == service and lower <= datetime.fromisoformat(o["uploaded_at"]) <= upper
    ]
    log_lines: list[str] = []
    for obj in objects:
        log_lines.extend(_format_docker_line(l) for l in _read_object_lines(bucket, obj["key"]) if l.strip())

    try:
        error_rate = query_prometheus_range(
            'sum(rate(agent_chat_requests_total{status="error"}[1m]))',
            minutes=window_minutes * 2,
            env=env,
        )
    except Exception as e:
        error_rate = {"error": str(e)}

    return {
        "service": service,
        "window": {"start": lower.isoformat(), "end": upper.isoformat()},
        "log_lines": log_lines[-200:],
        "error_rate_metric": error_rate,
    }


if __name__ == "__main__":
    mcp.run()
