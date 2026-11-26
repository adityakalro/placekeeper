"""Dispatch setup actions to the installed plugins."""
from pathlib import Path

from placekeeper.plugins.registry import installed


class Connections:
    def __init__(self, root, plugins=None):
        self.root = Path(root)
        self.plugins = installed() if plugins is None else plugins
        self.pending = self.root/'setup'
        self.pending.mkdir(mode=0o700, parents=True, exist_ok=True)

    def connected(self):
        return {key: plugin for key, plugin in self.plugins.items() if plugin.connected(self.root)}

    def status(self, services=None):
        services = self.connected() if services is None else services
        return {
            key: {'label': plugin.label, 'description': plugin.description, 'actions': plugin.actions,
                  'connected': key in services,
                  'can_disconnect': key in services or any((self.root/file).exists() for file in plugin.files)
                  or (self.pending/(key + '.json')).exists()}
            for key, plugin in self.plugins.items()
        }

    def ready(self, services=None):
        services = self.connected() if services is None else services
        return (sum(plugin.reader is not None for plugin in services.values()) >= 2
                and any(plugin.catalog_ready(services) for plugin in services.values()))

    def apply(self, service, action, data):
        if not isinstance(service, str) or service not in self.plugins:
            raise ValueError('Unknown service.')
        plugin = self.plugins[service]
        pending = self.pending/(service + '.json')
        if action == 'disconnect':
            if data.get('confirm') is not True:
                raise ValueError('Confirm disconnecting this service.')
            if plugin.disconnect:
                plugin.disconnect(self.root)
            for file in plugin.files:
                (self.root/file).unlink(missing_ok=True)
            pending.unlink(missing_ok=True)
            return {'connected': False}
        spec = next((item for item in plugin.actions if item['id'] == action), None)
        if spec is None or plugin.setup is None:
            raise ValueError('Unknown setup action.')
        if plugin.connected(self.root):
            raise ValueError('Disconnect this service before connecting another account.')
        if spec.get('after'):
            if not pending.exists():
                raise ValueError('Start a new connection first.')
        elif data.get('consent') is not True:
            raise ValueError('Confirm linking this account and saving its access on this installation.')
        return plugin.setup(self.root, pending, action, data)
