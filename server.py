#!/usr/bin/env python3
"""
server.py - Service Discovery System (Project 21) - Person 1

A server that advertises one or more services to the registry over UDP
every few seconds (the advertisement doubles as the heartbeat), and also
answers simple UDP requests on each service port so clients have something
to talk to directly after discovery (step 5 of the project flow).

Message format (agreed with the team):
    {"type":"advertise","name":"print","ip":"10.0.0.2","port":9000}

Examples:
    python3 server.py --service print:9000
    python3 server.py --service print:9000 --service storage:9001
    python3 server.py --service print:9000 --ip 10.0.0.2

Stop with Ctrl+C (or kill the process to simulate a crash: the registry
should drop it after the 15 s timeout).
"""

import argparse
import json
import socket
import sys
import threading
import time

REGISTRY_IP = "10.0.0.100"
REGISTRY_PORT = 5000
ADVERTISE_INTERVAL = 5  # seconds


# --------------------------------------------------------------------------
# Core logic (returns values, no printing)
# --------------------------------------------------------------------------
def build_advertisement(name, ip, port):
    """Return the advertisement message as a dict."""
    return {"type": "advertise", "name": name, "ip": ip, "port": port}


def encode(message):
    """Dict -> compact JSON bytes."""
    return json.dumps(message, separators=(",", ":")).encode("utf-8")


def parse_service(text):
    """Turn 'print:9000' into ('print', 9000). Raises ValueError if bad."""
    name, sep, port = text.partition(":")
    if not sep or not name.strip() or not port.isdigit():
        raise ValueError(f"bad service '{text}', expected NAME:PORT (e.g. print:9000)")
    port = int(port)
    if not 1 <= port <= 65535:
        raise ValueError(f"port out of range in '{text}'")
    return name.strip(), port


def detect_local_ip(registry_ip, registry_port):
    """
    Find which local IP is used to reach the registry. Connecting a UDP
    socket sends no packet; it only selects the outgoing interface.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect((registry_ip, registry_port))
        return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()


def make_response(name, ip, port, request):
    """Build the reply a service gives to a direct client request."""
    return {
        "type": "response",
        "name": name,
        "message": f"Hello from '{name}' server at {ip}:{port}",
        "echo": request.get("data", ""),
    }


# --------------------------------------------------------------------------
# Threads
# --------------------------------------------------------------------------
def advertise_loop(services, ip, registry_addr, interval, stop, log):
    """Send one advertisement per service every `interval` seconds."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        while not stop.is_set():
            for name, port in services:
                try:
                    sock.sendto(encode(build_advertisement(name, ip, port)), registry_addr)
                    log(f"advertised '{name}' at {ip}:{port} -> {registry_addr[0]}:{registry_addr[1]}")
                except OSError as err:
                    log(f"could not send advertisement for '{name}': {err}")
            stop.wait(interval)  # wakes immediately on shutdown
    finally:
        sock.close()


def serve_requests(name, port, ip, stop, log):
    """Answer direct UDP requests from clients on the service port."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind(("0.0.0.0", port))
    except OSError as err:
        log(f"cannot listen on port {port} for '{name}': {err}")
        stop.set()
        return
    sock.settimeout(1.0)
    while not stop.is_set():
        try:
            data, addr = sock.recvfrom(4096)
        except socket.timeout:
            continue
        except OSError:
            break
        try:
            request = json.loads(data.decode("utf-8"))
            if not isinstance(request, dict):
                raise ValueError
        except ValueError:
            request = {}
        log(f"'{name}' request from {addr[0]}:{addr[1]}")
        sock.sendto(encode(make_response(name, ip, port, request)), addr)
    sock.close()


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Service Discovery - server (advertiser)")
    parser.add_argument("--service", action="append", metavar="NAME:PORT",
                        help="service to offer; repeat for several (default: print:9000)")
    parser.add_argument("--ip", help="IP to advertise (default: auto-detect)")
    parser.add_argument("--registry-ip", default=REGISTRY_IP)
    parser.add_argument("--registry-port", type=int, default=REGISTRY_PORT)
    parser.add_argument("--interval", type=float, default=ADVERTISE_INTERVAL,
                        help="seconds between advertisements (default 5)")
    args = parser.parse_args()

    try:
        services = [parse_service(s) for s in (args.service or ["print:9000"])]
    except ValueError as err:
        parser.error(str(err))
    if len({port for _, port in services}) != len(services):
        parser.error("each service needs its own port")

    ip = args.ip or detect_local_ip(args.registry_ip, args.registry_port)
    registry_addr = (args.registry_ip, args.registry_port)
    stop = threading.Event()

    def log(msg):
        print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

    log(f"server up at {ip}, services: " + ", ".join(f"{n}:{p}" for n, p in services))

    threads = [threading.Thread(target=advertise_loop, daemon=True,
                                args=(services, ip, registry_addr, args.interval, stop, log))]
    for name, port in services:
        threads.append(threading.Thread(target=serve_requests, daemon=True,
                                        args=(name, port, ip, stop, log)))
    for t in threads:
        t.start()

    try:
        while not stop.is_set():
            time.sleep(0.5)
    except KeyboardInterrupt:
        log("shutting down")
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
