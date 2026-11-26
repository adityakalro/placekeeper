"""Read and write Kindle furthest-read positions."""

import re
from datetime import datetime, timezone
from xml.etree import ElementTree

from placekeeper.errors import MissingProgress, ReadFailure


KINDLE_URL = 'https://cde-ta-g7g.amazon.com/FionaCDEServiceEngine/sidecar'


def kindle_position(data, document, kind="PDOC"):
    payload = data.get('payload', {})
    if not isinstance(payload, dict) or not isinstance(payload.get('records'), list):
        raise ReadFailure('Kindle returned an unexpected progress structure')
    if payload.get('key') != document or payload.get('type') != kind:
        raise ReadFailure('Kindle response identifies a different document or type')
    values = []
    for record in payload.get('records', []):
        if not isinstance(record, dict):
            raise ReadFailure('Kindle returned an unexpected progress record')
        if record.get('type') == 'kindle.lpr':
            value = record.get('location')
            if not re.fullmatch(r'\d+', str(value)):
                raise ReadFailure('Kindle returned an invalid last-read position')
            values.append(int(value))
    if not values:
        raise MissingProgress('Open the book on Kindle and sync it first.')
    return max(values)


def read_sidecar(get, auth, document, kind="PDOC"):
    return get('Kindle', KINDLE_URL, auth=auth, params={'type': kind, 'key': document})


def write_position(client, auth, document, guid, target, kind="PDOC"):
    if not isinstance(guid, str) or not guid:
        raise ReadFailure('Kindle returned no document sync identifier')
    stamp = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S%z')
    root = ElementTree.Element('annotations', {'version': '1.0', 'timestamp': stamp})
    book = ElementTree.SubElement(root, 'book', {'key': document, 'type': kind, 'guid': guid})
    ElementTree.SubElement(book, 'last_read', {'action': 'modify', 'begin': str(target),
                                              'location': '-1', 'timestamp': stamp})
    return client.post(KINDLE_URL, auth=auth, content=ElementTree.tostring(root, encoding='utf-8'),
                       headers={'Content-Type': 'text/xml; charset=UTF-8'})
