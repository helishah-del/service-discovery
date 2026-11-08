
from os_ken.base import app_manager
from os_ken.controller import ofp_event
from os_ken.controller.handler import (
    CONFIG_DISPATCHER,
    MAIN_DISPATCHER,
    set_ev_cls
)
from os_ken.ofproto import ofproto_v1_3
from os_ken.lib.packet import packet, ethernet, ether_types


class LearningSwitch(app_manager.OSKenApp):

    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    # Registry configuration
    REGISTRY_IP = "10.0.0.100"
    REGISTRY_PORT = 5000
    REGISTRY_SWITCH_PORT = 3

    # Server IP -> switch port mapping
    SERVER_PORTS = {
        "10.0.0.2": 2,
        "10.0.0.4": 4,
        "10.0.0.5": 5
    }

    # Cookie used to identify flows managed by this application
    APP_COOKIE = 0x5344530000000001
    COOKIE_MASK = 0xFFFFFFFFFFFFFFFF

    def __init__(self, *args, **kwargs):
        super(LearningSwitch, self).__init__(*args, **kwargs)

        self.mac_to_port = {}
        self.datapaths = {}
        self.server_status = {}

        self.logger.info("Service Discovery SDN Controller started")

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

        # Registry rule:
        # UDP packets addressed to registry port 5000
        # are forwarded directly to switch port 3.
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

        self.server_status[server_ip] = True

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

        self.server_status[server_ip] = False

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
