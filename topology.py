#!/usr/bin/env python3
"""
topology.py - Mininet topology for Project 21 (Person 3)

Switch s1 ports (link order):
    1 h1 client   10.0.0.3
    2 h2 server   10.0.0.2
    3 h3 registry 10.0.0.100
    4 h4 server   10.0.0.4
    5 h5 server   10.0.0.5
    6 h6 client   10.0.0.6
    7 root namespace 10.0.0.1  (so the registry can send status UDP to the
                                controller, which runs outside Mininet)

controller.py uses these port numbers, so keep the link order unchanged.

Usage (start the controller first, from the project folder):
    osken-manager controller.py
    sudo python3 topology.py
"""

from mininet.cli import CLI
from mininet.log import setLogLevel
from mininet.net import Mininet
from mininet.node import Node, RemoteController
from mininet.topo import Topo


def connect_to_root_ns(net, switch, ip):
    """Link the root namespace to the switch (same idea as Mininet's
    examples/sshd.py) and start the network."""
    root = Node('root', inNamespace=False)
    intf = net.addLink(root, switch).intf1
    root.setIP(ip, intf=intf)
    net.start()
    return root


class ServiceDiscoveryTopo(Topo):

    def build(self):

        # Create one OpenFlow 1.3 switch
        s1 = self.addSwitch(
            's1',
            protocols='OpenFlow13'
        )

        # Two clients
        client1 = self.addHost(
            'h1',
            ip='10.0.0.3/24'
        )

        client2 = self.addHost(
            'h6',
            ip='10.0.0.6/24'
        )

        # Three service servers
        server1 = self.addHost(
            'h2',
            ip='10.0.0.2/24'
        )

        server2 = self.addHost(
            'h4',
            ip='10.0.0.4/24'
        )

        server3 = self.addHost(
            'h5',
            ip='10.0.0.5/24'
        )

        # Registry
        registry = self.addHost(
            'h3',
            ip='10.0.0.100/24'
        )

        # Connect all hosts to the switch
        self.addLink(client1, s1)
        self.addLink(server1, s1)
        self.addLink(registry, s1)
        self.addLink(server2, s1)
        self.addLink(server3, s1)
        self.addLink(client2, s1)


topos = {
    'servicediscovery': (
        lambda: ServiceDiscoveryTopo()
    )
}


def run():
    setLogLevel('info')

    net = Mininet(
        topo=ServiceDiscoveryTopo(),
        controller=None,
        autoSetMacs=True
    )

    net.addController(
        'c0',
        controller=RemoteController,
        ip='127.0.0.1',
        port=6653
    )

    # Give the root namespace an interface (10.0.0.1) on the switch so the
    # registry can reach the controller's UDP status listener (port 5001).
    # This call also starts the network.
    connect_to_root_ns(net, net.get('s1'), '10.0.0.1/24')

    CLI(net)
    net.stop()


if __name__ == '__main__':
    run()
