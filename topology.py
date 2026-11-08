
from mininet.topo import Topo


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
