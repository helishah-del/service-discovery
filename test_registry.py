#!/usr/bin/env python3
"""Tests for the current P2 registry.py implementation (macOS compatible)."""
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from registry import ServiceRegistry


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.registry = ServiceRegistry(
            bind_ip='127.0.0.1', port=0, timeout=0.15,
            check_interval=0.02, sync_interval=0.05,
            state_path=os.path.join(self.tmp.name, 'registry_state.json'),
            log_path=os.path.join(self.tmp.name, 'events.log'))

    def tearDown(self):
        self.registry.stop.set()
        if self.registry.sock:
            self.registry.sock.close()
        self.registry.notify_sock.close()
        self.tmp.cleanup()

    def test_multiple_servers_and_query(self):
        for ip in ('10.0.0.2', '10.0.0.4'):
            self.registry.register({'type': 'advertise', 'name': 'print', 'ip': ip, 'port': 9000})
        reply = self.registry.query('print', ('127.0.0.1', 12345))
        self.assertEqual(len(reply['services']), 2)
        self.assertEqual(self.registry.query('missing', ('127.0.0.1', 12345))['services'], [])
        with open(self.registry.state_path, encoding='utf-8') as file:
            self.assertEqual(len(json.load(file)['services']), 2)

    def test_heartbeat_and_expiration(self):
        msg = {'type': 'advertise', 'name': 'print', 'ip': '10.0.0.2', 'port': 9000}
        self.registry.register(msg)
        time.sleep(0.09)
        self.registry.register(msg)
        time.sleep(0.09)
        self.registry.expire_services()
        self.assertEqual(len(self.registry.services), 1)
        time.sleep(0.09)
        self.registry.expire_services()
        self.assertEqual(self.registry.query('print', ('127.0.0.1', 12345))['services'], [])

    def test_bad_advertisement(self):
        bad = [
            {'type': 'advertise', 'name': 'x', 'ip': 'bad', 'port': 9000},
            {'type': 'advertise', 'name': 'x', 'ip': '10.0.0.2', 'port': True},
        ]
        for msg in bad:
            self.registry.register(msg)
        self.assertEqual(len(self.registry.services), 0)

    def test_udp_round_trip(self):
        worker = threading.Thread(target=self.registry.run, daemon=True)
        worker.start()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(1.0)
        try:
            deadline = time.monotonic() + 2
            while self.registry.sock is None or not self.registry.sock.getsockname()[1]:
                if time.monotonic() > deadline:
                    self.fail('Registry did not start')
                time.sleep(0.01)
            addr = ('127.0.0.1', self.registry.sock.getsockname()[1])
            advertise = {'type': 'advertise', 'name': 'storage', 'ip': '127.0.0.1', 'port': 9001}
            sock.sendto(json.dumps(advertise).encode(), addr)
            sock.sendto(json.dumps({'type': 'query', 'name': 'storage'}).encode(), addr)
            reply, _ = sock.recvfrom(4096)
            self.assertEqual(json.loads(reply)['services'][0]['port'], 9001)
        finally:
            sock.close()
            self.registry.stop.set()
            worker.join(timeout=2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
