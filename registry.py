#!/usr/bin/env python3
"""Service Discovery System - Person 2: UDP service registry.

Receives P1 advertisements and discovery queries, expires silent services,
records events/state, and notifies P3's controller of server availability.

P3 must run a UDP status listener (default port 5001) that understands:
    {"type":"server_status","ip":"10.0.0.2","status":"live"}
    {"type":"server_status","ip":"10.0.0.2","status":"down"}

Example (inside registry Mininet host):
    python3 registry.py --bind 10.0.0.100 --controller-ip 10.0.0.1
Example (local development):
    python3 registry.py --bind 127.0.0.1 --controller-ip 127.0.0.1
"""

import argparse
import ipaddress
import json
import os
import socket
import threading
import time
from datetime import datetime, timezone

REGISTRY_IP = "10.0.0.100"
REGISTRY_PORT = 5000
CONTROLLER_PORT = 5001
TIMEOUT = 15.0
CHECK_INTERVAL = 1.0
SYNC_INTERVAL = 5.0


def encode(message):
    return json.dumps(message, separators=(",", ":")).encode("utf-8")


class ServiceRegistry:
    def __init__(self, bind_ip=REGISTRY_IP, port=REGISTRY_PORT,
                 controller_ip=None, controller_port=CONTROLLER_PORT,
                 timeout=TIMEOUT, check_interval=CHECK_INTERVAL,
                 sync_interval=SYNC_INTERVAL, state_path="registry_state.json",
                 log_path="events.log"):
        self.bind_ip = bind_ip
        self.port = port
        self.controller_addr = ((controller_ip, controller_port)
                                if controller_ip else None)
        self.timeout = timeout
        self.check_interval = check_interval
        self.sync_interval = sync_interval
        self.state_path = state_path
        self.log_path = log_path
        # Key by (service name, IP, port) so same-named services coexist.
        self.services = {}
        self.known_ips = set()
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.sock = None
        self.notify_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def log(self, message):
        line = f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {message}"
        print(line, flush=True)
        with self.lock:
            with open(self.log_path, "a", encoding="utf-8") as file:
                file.write(line + "\n")

    def save_state(self):
        with self.lock:
            entries = [
                {"name": name, "ip": ip, "port": port,
                 "last_seen": datetime.fromtimestamp(info["last_seen_wall"],
                                                      timezone.utc).isoformat()}
                for (name, ip, port), info in sorted(self.services.items())
            ]
            temp_path = self.state_path + ".tmp"
            with open(temp_path, "w", encoding="utf-8") as file:
                json.dump({"services": entries}, file, indent=2)
                file.write("\n")
            os.replace(temp_path, self.state_path)

    def notify_controller(self, ip, status):
        if self.controller_addr is None:
            return
        message = {"type": "server_status", "ip": ip, "status": status}
        try:
            self.notify_sock.sendto(encode(message), self.controller_addr)
        except OSError as error:
            self.log(f"Controller notification failed for {ip}: {error}")

    @staticmethod
    def valid_advertisement(message):
        if not isinstance(message, dict) or message.get("type") != "advertise":
            return False
        name, ip, port = (message.get("name"), message.get("ip"),
                          message.get("port"))
        if not isinstance(name, str) or not name.strip() or len(name) > 128:
            return False
        if not isinstance(ip, str):
            return False
        try:
            if ipaddress.ip_address(ip).version != 4:
                return False
        except ValueError:
            return False
        return type(port) is int and 1 <= port <= 65535

    def register(self, message):
        if not self.valid_advertisement(message):
            self.log("Ignored invalid advertisement")
            return
        name, ip, port = message["name"], message["ip"], message["port"]
        key = (name, ip, port)
        now = time.monotonic()
        with self.lock:
            # An IP is live if at least one of its services is active.
            was_live = any(k[1] == ip for k in self.services)
            is_new = key not in self.services
            self.services[key] = {"last_seen": now,
                                  "last_seen_wall": time.time()}
            self.known_ips.add(ip)
            self.save_state()
        if is_new:
            self.log(f"REGISTERED {name} at {ip}:{port}")
        if not was_live:
            self.log(f"Server {ip} LIVE")
            self.notify_controller(ip, "live")

    def query(self, name, addr):
        with self.lock:
            matches = [
                {"name": service, "ip": ip, "port": port}
                for (service, ip, port), _ in sorted(self.services.items())
                if service == name
            ]
        self.log(f"QUERIED '{name}' from {addr[0]}:{addr[1]} -> {len(matches)} result(s)")
        return {"type": "reply", "services": matches}

    def handle_packet(self, data, addr):
        try:
            message = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self.log(f"Ignored malformed JSON from {addr[0]}:{addr[1]}")
            return None
        if not isinstance(message, dict):
            self.log("Ignored non-object message")
            return None
        kind = message.get("type")
        if kind == "advertise":
            self.register(message)
            return None
        if kind == "query":
            name = message.get("name")
            if not isinstance(name, str) or not name.strip():
                self.log("Ignored invalid query")
                return None
            return self.query(name, addr)
        self.log(f"Ignored unknown message type: {kind!r}")
        return None

    def expire_services(self):
        now = time.monotonic()
        with self.lock:
            expired = [key for key, value in self.services.items()
                       if now - value["last_seen"] > self.timeout]
            if not expired:
                return
            for key in expired:
                del self.services[key]
            remaining_ips = {key[1] for key in self.services}
            down_ips = sorted({key[1] for key in expired} - remaining_ips)
            self.save_state()
        for name, ip, port in expired:
            self.log(f"TIMED OUT {name} at {ip}:{port}")
        for ip in down_ips:
            self.log(f"Server {ip} DOWN")
            self.notify_controller(ip, "down")

    def maintenance_loop(self):
        # Re-send status for known IPs so a controller that starts later can
        # learn live/down state. UDP notifications are best effort.
        next_sync = time.monotonic() + self.sync_interval
        while not self.stop.wait(self.check_interval):
            self.expire_services()
            if time.monotonic() >= next_sync:
                with self.lock:
                    live_ips = {key[1] for key in self.services}
                    known_ips = set(self.known_ips)
                for ip in sorted(known_ips):
                    self.notify_controller(ip, "live" if ip in live_ips else "down")
                next_sync = time.monotonic() + self.sync_interval

    def run(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock = sock
        try:
            sock.bind((self.bind_ip, self.port))
            sock.settimeout(1.0)
            self.save_state()
            worker = threading.Thread(target=self.maintenance_loop, daemon=True)
            worker.start()
            self.log(f"Registry listening on {self.bind_ip}:{self.port}")
            if self.controller_addr:
                self.log(f"Controller notifications -> {self.controller_addr[0]}:{self.controller_addr[1]}")
            while not self.stop.is_set():
                try:
                    data, addr = sock.recvfrom(65535)
                except socket.timeout:
                    continue
                except OSError:
                    if self.stop.is_set():
                        break
                    raise
                response = self.handle_packet(data, addr)
                if response is not None:
                    try:
                        sock.sendto(encode(response), addr)
                    except OSError as error:
                        self.log(f"Failed to reply to {addr}: {error}")
        finally:
            self.stop.set()
            sock.close()
            self.notify_sock.close()
            self.sock = None


def main():
    parser = argparse.ArgumentParser(description="Service Discovery - P2 registry")
    parser.add_argument("--bind", default=REGISTRY_IP,
                        help="local address to listen on (default 10.0.0.100)")
    parser.add_argument("--port", type=int, default=REGISTRY_PORT)
    parser.add_argument("--controller-ip", default=None,
                        help="IP of P3 controller's UDP status listener; omit to disable")
    parser.add_argument("--controller-port", type=int, default=CONTROLLER_PORT)
    parser.add_argument("--timeout", type=float, default=TIMEOUT)
    parser.add_argument("--check-interval", type=float, default=CHECK_INTERVAL)
    parser.add_argument("--sync-interval", type=float, default=SYNC_INTERVAL)
    parser.add_argument("--state", default="registry_state.json")
    parser.add_argument("--log", default="events.log")
    args = parser.parse_args()
    if args.timeout <= 0 or args.check_interval <= 0 or args.sync_interval <= 0:
        parser.error("timeout and intervals must be positive")
    registry = ServiceRegistry(
        bind_ip=args.bind, port=args.port,
        controller_ip=args.controller_ip, controller_port=args.controller_port,
        timeout=args.timeout, check_interval=args.check_interval,
        sync_interval=args.sync_interval, state_path=args.state,
        log_path=args.log)
    try:
        registry.run()
    except KeyboardInterrupt:
        print("\nRegistry shutting down", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
