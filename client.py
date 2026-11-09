#!/usr/bin/env python3
"""
client.py - Service Discovery System (Project 21) - Person 1

Sends UDP discovery queries to the registry, prints the replies, measures
the discovery latency of every query and saves it to latency.csv.

Message formats (agreed with the team):
    Query: {"type":"query","name":"print"}
    Reply: {"type":"reply","services":[{"name":"print","ip":"10.0.0.2","port":9000}]}

Latency = (t2 - t1) * 1000 ms, where t1 is taken just before the query is
sent and t2 just after the reply arrives (time.perf_counter()).

Examples:
    python3 client.py print                      # 10 queries, 1 s apart
    python3 client.py print --count 50 --delay 0.2
    python3 client.py print --count 1 --connect  # also contact the server found
    python3 client.py print --label "5_servers"  # tag runs for comparison

Run inside a Mininet host with:  h4 python3 client.py print
"""

import argparse
import csv
import json
import os
import socket
import statistics
import sys
import time

REGISTRY_IP = "10.0.0.100"
REGISTRY_PORT = 5000
QUERY_TIMEOUT = 2.0  # seconds to wait for a reply
CSV_FIELDS = ["timestamp", "client", "service", "servers_found",
              "latency_ms", "status", "label"]


# --------------------------------------------------------------------------
# Core logic (returns values, no printing)
# --------------------------------------------------------------------------
def build_query(name):
    return {"type": "query", "name": name}


def query_registry(name, registry_ip=REGISTRY_IP, registry_port=REGISTRY_PORT,
                   timeout=QUERY_TIMEOUT):
    """
    Ask the registry who offers `name`.

    Returns a dict:
        {"status": "ok" | "timeout" | "bad_reply" | "error",
         "services": [ {name, ip, port}, ... ],
         "latency_ms": float or None}
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    payload = json.dumps(build_query(name), separators=(",", ":")).encode("utf-8")
    try:
        t1 = time.perf_counter()
        sock.sendto(payload, (registry_ip, registry_port))
        data, _ = sock.recvfrom(65535)
        t2 = time.perf_counter()
    except socket.timeout:
        return {"status": "timeout", "services": [], "latency_ms": None}
    except OSError:
        return {"status": "error", "services": [], "latency_ms": None}
    finally:
        sock.close()

    latency_ms = (t2 - t1) * 1000.0
    try:
        reply = json.loads(data.decode("utf-8"))
        if reply.get("type") != "reply" or not isinstance(reply.get("services"), list):
            raise ValueError
    except (ValueError, AttributeError):
        return {"status": "bad_reply", "services": [], "latency_ms": latency_ms}
    return {"status": "ok", "services": reply["services"], "latency_ms": latency_ms}


def contact_server(service, data="hello", timeout=QUERY_TIMEOUT):
    """
    Talk to a discovered server directly (registry not involved).
    Returns the server's reply dict, or None on failure.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        request = {"type": "request", "name": service["name"], "data": data}
        sock.sendto(json.dumps(request).encode("utf-8"),
                    (service["ip"], int(service["port"])))
        reply, _ = sock.recvfrom(4096)
        return json.loads(reply.decode("utf-8"))
    except (OSError, ValueError, KeyError):
        return None
    finally:
        sock.close()


def save_latency(csv_path, client_id, name, result, label=""):
    """Append one measurement row to the CSV file (creates header if new)."""
    new_file = not os.path.exists(csv_path) or os.path.getsize(csv_path) == 0
    with open(csv_path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if new_file:
            writer.writeheader()
        writer.writerow({
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "client": client_id,
            "service": name,
            "servers_found": len(result["services"]),
            "latency_ms": "" if result["latency_ms"] is None else f"{result['latency_ms']:.3f}",
            "status": result["status"],
            "label": label,
        })


def summarize(latencies):
    """Return count/min/avg/median/max for a list of latencies (ms)."""
    if not latencies:
        return None
    return {
        "count": len(latencies),
        "min": min(latencies),
        "avg": statistics.mean(latencies),
        "median": statistics.median(latencies),
        "max": max(latencies),
    }


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Service Discovery - client (queries + latency)")
    parser.add_argument("service", help="service name to look for, e.g. print")
    parser.add_argument("--registry-ip", default=REGISTRY_IP)
    parser.add_argument("--registry-port", type=int, default=REGISTRY_PORT)
    parser.add_argument("--count", type=int, default=10, help="number of queries (default 10)")
    parser.add_argument("--delay", type=float, default=1.0, help="seconds between queries (default 1)")
    parser.add_argument("--timeout", type=float, default=QUERY_TIMEOUT)
    parser.add_argument("--csv", default="latency.csv", help="output file (default latency.csv)")
    parser.add_argument("--client-id", default=socket.gethostname())
    parser.add_argument("--label", default="",
                        help="tag for this run, e.g. '1_server', '5_servers', 'with_sdn'")
    parser.add_argument("--connect", action="store_true",
                        help="after the last query, contact the first server found")
    args = parser.parse_args()

    latencies = []
    last_result = None
    for i in range(1, args.count + 1):
        result = query_registry(args.service, args.registry_ip, args.registry_port, args.timeout)
        save_latency(args.csv, args.client_id, args.service, result, args.label)
        last_result = result

        if result["status"] == "ok":
            latencies.append(result["latency_ms"])
            found = ", ".join(f"{s.get('ip')}:{s.get('port')}" for s in result["services"]) or "none"
            print(f"[{i}/{args.count}] '{args.service}' -> {len(result['services'])} server(s) "
                  f"[{found}]  latency = {result['latency_ms']:.3f} ms")
        else:
            print(f"[{i}/{args.count}] '{args.service}' -> {result['status'].upper()}")

        if i < args.count:
            time.sleep(args.delay)

    stats = summarize(latencies)
    if stats:
        print(f"\nSummary over {stats['count']} reply(ies): min {stats['min']:.3f} ms, "
              f"avg {stats['avg']:.3f} ms, median {stats['median']:.3f} ms, "
              f"max {stats['max']:.3f} ms")
    else:
        print("\nNo successful replies received.")
    print(f"Saved to {args.csv}")

    if args.connect and last_result and last_result["services"]:
        target = last_result["services"][0]
        print(f"\nContacting {target['name']} at {target['ip']}:{target['port']} directly...")
        reply = contact_server(target)
        print(f"Server replied: {reply['message']}" if reply else "No reply from server.")

    return 0 if stats else 1


if __name__ == "__main__":
    sys.exit(main())
