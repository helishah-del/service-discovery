"""
controller.py - Service Discovery SDN controller (Person 3), os-ken / OpenFlow 1.3

Flow rules (match the project guide):
  priority 200  ipv4_dst = <dead server>             -> DROP
  priority 100  ipv4_dst = <live server>             -> forward to its switch port
  priority 100  udp, ipv4_dst=registry, udp_dst=5000 -> forward to registry port
  priority   1  in_port + eth_dst (learned MAC)      -> forward (learning switch)
  priority   0  anything else                        -> send to controller

The registry (P2) tells this controller about server availability with UDP
messages sent to <controller-ip>:5001:
    {"type":"server_status","ip":"10.0.0.2","status":"live"}
    {"type":"server_status","ip":"10.0.0.2","status":"down"}
The registry re-sends the status of every known server every ~5 s, so this
controller only acts when a server's state actually CHANGES.

Run:
    osken-manager controller.py
Without the discovery rules (learning switch only, for latency comparison):
    SD_RULES=0 osken-manager controller.py
"""

import json
import os
import socket

from os_ken.base import app_manager
from os_ken.controller import ofp_event
from os_ken.controller.handler import (
    CONFIG_DISPATCHER,
    MAIN_DISPATCHER,
    set_ev_cls
)
from os_ken.lib import hub
from os_ken.ofproto import ofproto_v1_3
from os_ken.lib.packet import packet, ethernet, ether_types


class LearningSwitch(app_manager.OSKenApp):

    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    # Registry configuration
    REGISTRY_IP = "10.0.0.100"
    REGISTRY_PORT = 5000
    REGISTRY_SWITCH_PORT = 3

    # Server IP -> switch port mapping (must match topology.py link order)
    SERVER_PORTS = {
        "10.0.0.2": 2,
        "10.0.0.4": 4,
        "10.0.0.5": 5
    }

    # UDP listener for registry -> controller status messages
    STATUS_BIND = "0.0.0.0"
    STATUS_PORT = 5001

    # SD_RULES=0 disables the discovery-specific rules (latency comparison)
    RULES_ENABLED = os.environ.get("SD_RULES", "1") != "0"

    # Cookie used to identify flows managed by this application
    APP_COOKIE = 0x5344530000000001
    COOKIE_MASK = 0xFFFFFFFFFFFFFFFF

    def __init__(self, *args, **kwargs):
        super(LearningSwitch, self).__init__(*args, **kwargs)

        self.mac_to_port = {}
        self.datapaths = {}
        self.server_status = {}

        self.logger.info(
            "Service Discovery SDN Controller started (discovery rules %s)",
            "ON" if self.RULES_ENABLED else "OFF"
        )

        hub.spawn(self.status_listener)

    # --------------------------------------------------
    # FLOW INSTALLATION
    # --------------------------------------------------

    def add_flow(
        self,
        datapath,
        priority,
        match,
        actions=None,
        cookie=APP_COOKIE
    ):
        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto

        instructions = []

        # None means an explicit drop rule: no instructions.
        # An empty action list also means drop.
        if actions:
            instructions = [
                parser.OFPInstructionActions(
                    ofproto.OFPIT_APPLY_ACTIONS,
                    actions
                )
            ]

        flow_mod = parser.OFPFlowMod(
            datapath=datapath,
            cookie=cookie,
            priority=priority,
            match=match,
            instructions=instructions
        )

        datapath.send_msg(flow_mod)

    def delete_flow(self, datapath, match):
        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto

        flow_mod = parser.OFPFlowMod(
            datapath=datapath,
            cookie=self.APP_COOKIE,
            cookie_mask=self.COOKIE_MASK,
            command=ofproto.OFPFC_DELETE,
            table_id=ofproto.OFPTT_ALL,
            out_port=ofproto.OFPP_ANY,
            out_group=ofproto.OFPG_ANY,
            match=match
        )

        datapath.send_msg(flow_mod)

    # --------------------------------------------------
    # SWITCH CONNECTION
    # --------------------------------------------------

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        datapath = ev.msg.datapath
        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto

        self.datapaths[datapath.id] = datapath

        self.logger.info(
            "Switch connected: datapath ID %s",
            datapath.id
        )

        # Default rule: send unknown packets to controller.
        match = parser.OFPMatch()
        actions = [
            parser.OFPActionOutput(
                ofproto.OFPP_CONTROLLER,
                ofproto.OFPCML_NO_BUFFER
            )
        ]

        self.add_flow(
            datapath,
            priority=0,
            match=match,
            actions=actions,
            cookie=0
        )

        if not self.RULES_ENABLED:
            return

        # Registry rule:
        # UDP packets addressed to registry port 5000
        # are forwarded directly to the registry's switch port.
        registry_match = parser.OFPMatch(
            eth_type=ether_types.ETH_TYPE_IP,
            ipv4_dst=self.REGISTRY_IP,
            ip_proto=17,
            udp_dst=self.REGISTRY_PORT
        )

        registry_actions = [
            parser.OFPActionOutput(self.REGISTRY_SWITCH_PORT)
        ]

        self.add_flow(
            datapath,
            priority=100,
            match=registry_match,
            actions=registry_actions
        )

        # Restore server rules if the switch reconnects.
        for server_ip, alive in self.server_status.items():
            if alive:
                self.add_server_flow(datapath, server_ip)
            else:
                self.add_drop_flow(datapath, server_ip)

    # --------------------------------------------------
    # SERVER FLOW RULES
    # --------------------------------------------------

    def add_server_flow(self, datapath, server_ip):
        parser = datapath.ofproto_parser

        switch_port = self.SERVER_PORTS.get(server_ip)

        if switch_port is None:
            self.logger.warning(
                "Unknown server IP: %s",
                server_ip
            )
            return

        match = parser.OFPMatch(
            eth_type=ether_types.ETH_TYPE_IP,
            ipv4_dst=server_ip
        )

        actions = [
            parser.OFPActionOutput(switch_port)
        ]

        # Priority 100: forward traffic to a live server.
        self.add_flow(
            datapath,
            priority=100,
            match=match,
            actions=actions
        )

        self.logger.info(
            "LIVE server %s -> switch port %s",
            server_ip,
            switch_port
        )

    def add_drop_flow(self, datapath, server_ip):
        parser = datapath.ofproto_parser

        match = parser.OFPMatch(
            eth_type=ether_types.ETH_TYPE_IP,
            ipv4_dst=server_ip
        )

        # Priority 200: drop traffic to a dead server.
        self.add_flow(
            datapath,
            priority=200,
            match=match,
            actions=[]
        )

        self.logger.info(
            "DROP traffic to dead server %s",
            server_ip
        )

    # --------------------------------------------------
    # SERVER STATUS UPDATES
    # --------------------------------------------------

    def add_server(self, server_ip):
        if server_ip not in self.SERVER_PORTS:
            self.logger.warning(
                "Cannot activate unknown server: %s",
                server_ip
            )
            return

        # The registry repeats status every few seconds: ignore no-ops so
        # flows are not deleted and re-added for nothing.
        if self.server_status.get(server_ip) is True:
            return

        self.server_status[server_ip] = True

        if not self.RULES_ENABLED:
            return

        for datapath in self.datapaths.values():
            parser = datapath.ofproto_parser

            match = parser.OFPMatch(
                eth_type=ether_types.ETH_TYPE_IP,
                ipv4_dst=server_ip
            )

            # Remove previous live/drop rule before adding
            # the new live forwarding rule.
            self.delete_flow(datapath, match)
            self.add_server_flow(datapath, server_ip)

        self.logger.info("Server ACTIVE: %s", server_ip)

    def block_server(self, server_ip):
        if server_ip not in self.SERVER_PORTS:
            self.logger.warning(
                "Cannot block unknown server: %s",
                server_ip
            )
            return

        if self.server_status.get(server_ip) is False:
            return

        self.server_status[server_ip] = False

        if not self.RULES_ENABLED:
            return

        for datapath in self.datapaths.values():
            parser = datapath.ofproto_parser

            match = parser.OFPMatch(
                eth_type=ether_types.ETH_TYPE_IP,
                ipv4_dst=server_ip
            )

            # Remove previous forwarding/drop rule first.
            self.delete_flow(datapath, match)
            self.add_drop_flow(datapath, server_ip)

        self.logger.info("Server BLOCKED: %s", server_ip)

    def remove_server_rules(self, server_ip):
        if server_ip not in self.SERVER_PORTS:
            self.logger.warning(
                "Unknown server: %s",
                server_ip
            )
            return

        self.server_status.pop(server_ip, None)

        for datapath in self.datapaths.values():
            parser = datapath.ofproto_parser

            match = parser.OFPMatch(
                eth_type=ether_types.ETH_TYPE_IP,
                ipv4_dst=server_ip
            )

            self.delete_flow(datapath, match)

        self.logger.info(
            "Removed controller rules for %s",
            server_ip
        )

    # --------------------------------------------------
    # REGISTRY STATUS LISTENER (UDP 5001)
    # --------------------------------------------------

    def handle_status_message(self, message):
        """Apply one server_status message from the registry."""
        if not isinstance(message, dict):
            return
        if message.get("type") != "server_status":
            return

        ip = message.get("ip")
        status = message.get("status")

        if status == "live":
            self.add_server(ip)
        elif status == "down":
            self.block_server(ip)
        else:
            self.logger.warning("Unknown status message: %s", message)

    def status_listener(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        try:
            sock.bind((self.STATUS_BIND, self.STATUS_PORT))
        except OSError as error:
            self.logger.error(
                "Cannot listen on UDP %s:%s: %s",
                self.STATUS_BIND,
                self.STATUS_PORT,
                error
            )
            return

        self.logger.info(
            "Listening for registry status on UDP %s:%s",
            self.STATUS_BIND,
            self.STATUS_PORT
        )

        while True:
            try:
                data, addr = sock.recvfrom(4096)
                self.handle_status_message(json.loads(data.decode("utf-8")))
            except (ValueError, UnicodeDecodeError):
                self.logger.warning("Ignored malformed status message")
            except Exception as error:  # keep the listener alive
                self.logger.error("Status listener error: %s", error)

    # --------------------------------------------------
    # PACKET-IN: LEARNING SWITCH
    # --------------------------------------------------

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg = ev.msg
        datapath = msg.datapath
        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto

        in_port = msg.match["in_port"]

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)

        if eth is None:
            return

        # Ignore LLDP packets.
        if eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return

        dst = eth.dst
        src = eth.src
        dpid = datapath.id

        self.mac_to_port.setdefault(dpid, {})

        # Learn source MAC address.
        self.mac_to_port[dpid][src] = in_port

        # Find destination MAC address.
        out_port = self.mac_to_port[dpid].get(
            dst,
            ofproto.OFPP_FLOOD
        )

        actions = [
            parser.OFPActionOutput(out_port)
        ]

        # Install a MAC forwarding rule when destination is known.
        if out_port != ofproto.OFPP_FLOOD:
            match = parser.OFPMatch(
                in_port=in_port,
                eth_dst=dst
            )

            self.add_flow(
                datapath,
                priority=1,
                match=match,
                actions=actions,
                cookie=0
            )

        # Forward packet.
        data = None

        if msg.buffer_id == ofproto.OFP_NO_BUFFER:
            data = msg.data

        out = parser.OFPPacketOut(
            datapath=datapath,
            buffer_id=msg.buffer_id,
            in_port=in_port,
            actions=actions,
            data=data
        )

        datapath.send_msg(out)
