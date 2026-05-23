#!/usr/bin/env python3
"""
Minimal Python equivalent of query.ps1.

Run with: python query.py
Edit HOST / PORT if needed. Requires Python 3.10+.
"""
import socket
import json
import datetime as dt
import sys

HOST = "192.168.1.203"
PORT = 9000
READ_TIMEOUT = 30.0

def hae_ts(d: dt.datetime) -> str:
    return d.strftime("%Y-%m-%d %H:%M:%S %z")

def call(name: str, arguments: dict, request_id: int = 1) -> dict:
    req = json.dumps({
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "callTool",
        "params": {"name": name, "arguments": arguments},
    }) + "\n"
    s = socket.socket()
    s.settimeout(READ_TIMEOUT)
    s.connect((HOST, PORT))
    try:
        s.sendall(req.encode("utf-8"))
        buf = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
            try:
                return json.loads(buf.decode("utf-8").strip())
            except json.JSONDecodeError:
                continue
    finally:
        s.close()
    raise RuntimeError("no parseable response")

def extract_payload(resp: dict) -> dict:
    result = resp.get("result") or {}
    if "data" in result:
        return result["data"]
    content = result.get("content") or []
    if content and isinstance(content[0], dict) and "text" in content[0]:
        return json.loads(content[0]["text"]).get("data", {})
    return {}

def main() -> int:
    now = dt.datetime.now().astimezone()
    end = hae_ts(now)
    start_30 = hae_ts(now - dt.timedelta(days=30))
    start_7 = hae_ts(now - dt.timedelta(days=7))

    probes = [
        ("ecg", {"start": start_30, "end": end}),
        ("heart_notifications", {"start": start_30, "end": end}),
        ("workouts", {"start": start_30, "end": end,
                      "includeMetadata": False, "includeRoutes": False}),
        ("health_metrics", {"start": start_7, "end": end,
                            "metrics": "step_count,active_energy,heart_rate,resting_heart_rate",
                            "interval": "days", "aggregate": True}),
    ]

    for i, (name, args) in enumerate(probes, start=1):
        print("=" * 70)
        print(f"{i}. {name}  args={args}")
        try:
            resp = call(name, args, request_id=i)
            if "error" in resp:
                print(f"  ERROR: {resp['error']}")
                continue
            payload = extract_payload(resp)
            print(f"  keys: {list(payload.keys())}")
            for k, v in payload.items():
                n = len(v) if hasattr(v, "__len__") else "?"
                print(f"    {k}: {n} item(s)")
        except Exception as exc:
            print(f"  EXCEPTION: {type(exc).__name__}: {exc}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
