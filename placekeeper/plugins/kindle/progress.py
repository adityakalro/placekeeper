"""Read Amazon's minute-resolution furthest-read time for a document."""

import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
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


def parse_progress_time(xml, server_date, expected_position):
    server_time = parsedate_to_datetime(server_date).astimezone(timezone.utc)
    try:
        records = list(ElementTree.fromstring(xml).iter('last_read'))
    except ElementTree.ParseError:
        raise ValueError('Amazon returned invalid last-read XML') from None
    if len(records) != 1:
        raise ValueError('Amazon returned an ambiguous last-read record')
    record = records[0]
    if record.get('method') != 'FRL' or int(record.get('pos', '-1')) != expected_position:
        raise ValueError('Amazon last-read position changed during the check')
    message = ''.join(record.itertext())
    match = re.search(r' at (\d{1,2}):(\d{2}) (AM|PM) GMT (today|yesterday|on [A-Za-z]+ \d{1,2}, \d{4})\.', message)
    if match is None:
        raise ValueError('Amazon last-read date format is unsupported')
    hour, minute = int(match[1]), int(match[2])
    if not 1 <= hour <= 12 or not 0 <= minute < 60:
        raise ValueError('Amazon returned an invalid last-read time')
    hour = hour % 12 + (12 if match[3] == 'PM' else 0)
    if match[4].startswith('on '):
        months = 'January February March April May June July August September October November December'.split()
        month, day_number, year = match[4][3:].replace(',', '').split()
        day = datetime(int(year), months.index(month) + 1, int(day_number)).date()
    else:
        day = server_time.date() - timedelta(days=match[4] == 'yesterday')
    timestamp = datetime(day.year, day.month, day.day, hour, minute, tzinfo=timezone.utc)
    if timestamp > server_time:
        raise ValueError('Amazon returned a future last-read time')
    return timestamp


def fetch_progress_time(sources, profile, sidecar, position):
    response = sources.client.get(
        'https://cde-ta-g7g.amazon.com/FionaCDEServiceEngine/getAnnotations',
        auth=sources.auth,
        params={'filter': 'last_read', 'type': profile.get('kind', 'PDOC'), 'key': profile['document'],
                'guid': sidecar['payload']['guid']},
    )
    if response.status_code != 200:
        raise ValueError(f'Amazon last-read time returned HTTP {response.status_code}')
    return parse_progress_time(response.content, response.headers['date'], position)
