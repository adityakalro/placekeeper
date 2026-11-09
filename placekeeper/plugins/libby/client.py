"""Libby device session and chapter timing for explicit Placekeeper jobs."""
import base64
import json
import math
import re
import uuid
from pathlib import Path
from urllib.parse import urlsplit
import time
from xml.etree import ElementTree
from zipfile import ZipFile

import httpx
from placekeeper.errors import MissingProgress, ReadFailure
from placekeeper.storage import private_json


def extract_timing(page, host):
    match = re.fullmatch(r'dewey-([a-f0-9]+)\.listen\.libbyapp\.com', host)
    encoded = re.search(r'\}\)\((\[".*?"\])\);SPARK.bifocalPath', page)
    if not match or not encoded:
        raise ReadFailure('Libby player metadata format changed.')
    digits = [int(char) if char.isdigit() else 0 for char in reversed(match[1])]
    decoded = []
    for index, char in enumerate('"'.join(json.loads(encoded[1]))):
        digit = digits[index % len(digits)]
        code = ord(char) + ((index + digit) % 94 if digit else 0)
        decoded.append(chr(code % 126 + 32 if digit and code > 126 else code))
    book = json.loads(base64.b64decode(''.join(decoded), validate=True))['b']
    parts, paths, offset = [], {}, 0
    for index, row in enumerate(book['spine']):
        duration = round(float(row['audio-duration']) * 1000)
        if duration <= 0:
            raise ReadFailure('Libby returned an invalid audio duration.')
        part = {'spine': int(row.get('-odread-spine-position', index)),
                'start': offset, 'duration': duration}
        parts.append(part)
        for name in ('path', '-odread-original-path'):
            if row.get(name):
                paths[row[name].lstrip('/')] = part
        offset += duration

    def walk(rows):
        for row in rows:
            path, _, seconds = row['path'].partition('#')
            start = paths[path.lstrip('/')]['start'] + round(float(seconds or 0) * 1000)
            yield [row['title'], start]
            yield from walk(row.get('contents') or [])

    toc = list(walk(book['nav']['toc']))
    if (not toc or any(not 0 <= start < offset for _, start in toc)
            or [start for _, start in toc] != sorted(start for _, start in toc)):
        raise ReadFailure('Libby chapter times are invalid.')
    return {'chapters': toc, 'parts': parts, 'duration': offset}


class Libby:
    BASE = 'https://sentry.libbyapp.com'

    def __init__(self, token_path):
        self.path = Path(token_path)
        self.state = json.loads(self.path.read_text()) if self.path.exists() else {}
        self.client = httpx.Client(timeout=30, headers={
            'Accept': 'application/json', 'Origin': 'https://libbyapp.com',
            'Referer': 'https://libbyapp.com/', 'User-Agent': 'CrossPoint',
            'Sec-Fetch-Dest': 'empty', 'Sec-Fetch-Mode': 'cors',
            'Sec-Fetch-Site': 'same-site', 'Cache-Control': 'no-cache'})
        self.loans = None

    def close(self):
        self.client.close()

    def refresh(self, anonymous=False):
        identity = None if anonymous else self.state.get('identity')
        seed = identity or 'cudlkahllcnsjxhbmddl'
        language = ''.join(c for c in seed if 'a' <= c <= 'z')[::-1][4:6]
        params = {'c': 'd:22.0.3', 's': '0'}
        headers = {'Accept-Language': language}
        if identity:
            part = identity.split('.')[1]
            payload = json.loads(base64.urlsafe_b64decode(part + '=' * (-len(part) % 4)))
            params['v'] = str(payload['chip']['id']).split('-')[0]
            headers['Authorization'] = 'Bearer ' + identity
        response = self.client.post(self.BASE + '/chip',
            params=params, headers=headers)
        if response.status_code != 200:
            raise ReadFailure('Libby session refresh failed. Relink Libby.')
        data = response.json()
        self.state.update(identity=data['identity'], chipId=data['chip'])
        private_json(self.path, self.state)

    def pairing_start(self):
        self.refresh(anonymous=True)
        data = self.get('/chip/clone/code?code=&role=pointer')
        code = str(data['code'])
        if not re.fullmatch(r'\d{8}', code):
            raise ReadFailure('Libby returned an invalid setup code.')
        self.state.update(code=code, expires=time.time()+300)
        private_json(self.path, self.state)
        return {'code': code}

    def pairing_finish(self, destination):
        data = self.get('/chip/clone/code?code=' + self.state['code'] + '&role=pointer')
        if data.get('result') != 'fulfilled' or not data.get('blessing'):
            raise ReadFailure('Enter the setup code in Libby, then check again. The code may have expired.')
        response = self.client.post(self.BASE + '/chip/clone', json={'blessing': data['blessing']},
            headers={'Authorization': 'Bearer ' + self.state['identity']})
        if response.status_code != 200:
            raise ReadFailure('Libby did not complete linking. Start a new setup code.')
        self.refresh()
        self.get('/chip/sync')
        private_json(destination, {'identity': self.state['identity'],
                                  'chipId': self.state['chipId'], 'paired': True})
        self.path.unlink(missing_ok=True)

    def get(self, path, *, allow_missing_position=False):
        for attempt in range(2):
            try:
                response = self.client.get(self.BASE + path,
                    headers={'Authorization': 'Bearer ' + self.state['identity']})
                if response.status_code == 403 and attempt == 0 and response.json().get('result') == 'missing_chip':
                    self.refresh()
                    continue
                if allow_missing_position and response.status_code == 404:
                    if response.json().get('result') == 'missing_stamp':
                        return {}
                if response.status_code != 200:
                    raise ReadFailure(f'Libby read returned HTTP {response.status_code}.')
                return response.json()
            except httpx.HTTPError:
                raise ReadFailure('Could not reach Libby.') from None

    def audiobook_loans(self):
        if self.loans is None:
            self.loans = self.get('/chip/sync')['loans']
        return [loan for loan in self.loans if loan.get('type', {}).get('id') == 'audiobook']

    def match(self, title, epub):
        def normalize(value):
            return re.sub(r'[^\w]+', '', value.casefold())
        candidates = [loan for loan in self.audiobook_loans() if normalize(loan.get('title', '')) == normalize(title)]
        if len(candidates) > 1:
            raise ReadFailure('More than one Libby audiobook matches this title.')
        if not candidates:
            return None
        with ZipFile(epub) as book:
            container = ElementTree.fromstring(book.read('META-INF/container.xml'))
            opf = container.find('.//{*}rootfile').get('full-path')
            metadata = ElementTree.fromstring(book.read(opf))
            authors = [''.join(node.itertext()).strip() for node in metadata.findall('.//{*}creator')]
        loan = candidates[0]
        creators = [loan.get('firstCreatorName', ''), loan.get('creator', '')]
        creators.extend(item.get('name', '') for item in loan.get('creators', []) if isinstance(item, dict))
        def author_key(value):
            return tuple(sorted(re.findall(r'\w+', value.casefold()))) if isinstance(value, str) else ()
        if not any(author_key(author) and author_key(author) == author_key(creator)
                   for author in authors for creator in creators):
            raise ReadFailure('Libby title matches, but its author could not be verified.')
        return loan

    def position(self, loan):
        data = self.get(f"/card/{loan['cardId']}/audiobook/data/{loan['id']}", allow_missing_position=True)
        marker = data.get('position')
        if marker is None or marker == {}:
            raise MissingProgress('Open the audiobook in Libby and sync your listening position first.')
        return marker

    def open_player(self, loan):
        meta = self.get(f"/open/audiobook/card/{loan['cardId']}/title/{loan['id']}")
        url = meta['urls']['web']
        parsed = urlsplit(url)
        if (parsed.scheme != 'https' or not re.fullmatch(r'dewey-[a-f0-9]+\.listen\.libbyapp\.com', parsed.netloc)
                or parsed.path != '/' or parsed.query or parsed.fragment):
            raise ReadFailure('Libby returned an unexpected player URL.')
        player = httpx.Client(timeout=30)
        try:
            response = player.get(url + '?' + meta['message'])
            if response.status_code != 200:
                raise ReadFailure('Could not open the Libby audiobook session.')
            timing = extract_timing(response.text, parsed.hostname)
            return player, url, timing
        except httpx.HTTPError:
            player.close()
            raise ReadFailure('Could not reach the Libby audiobook player.') from None
        except Exception:
            player.close()
            raise

    def write(self, player, url, timing, target):
        if not 0 <= target < timing['duration']:
            raise ValueError('Audio position is outside the recording')
        part = next(p for p in timing['parts'] if p['start'] <= target < p['start'] + p['duration'])
        offset = target - part['start']
        stamp = time.time()
        record = {'spinePosition': part['spine'], 'componentMilliseconds': offset,
                  'percentageOfComponent': offset / part['duration'],
                  'percentageOfBook': target / timing['duration'],
                  'timestamp': round(stamp), 'syncstamp': round(stamp * 1000), 'uuid': str(uuid.uuid4())}
        response = player.post(url + '_d/activity', json={
            'environment': {'deviceId': self.state['chipId'], 'timezoneOffset': 0,
                            'display': {'width': 0, 'height': 0}},
            'activities': {'position': [record]}},
            headers={'Accept': 'application/json', 'Origin': url.rstrip('/'), 'Referer': url})
        if response.status_code != 200:
            raise ReadFailure(f'Libby update returned HTTP {response.status_code}.')


def elapsed(timing, marker):
    part = next(p for p in timing['parts'] if p['spine'] == marker['spinePosition'])
    offset = marker['componentMilliseconds']
    if not isinstance(offset, (int, float)) or not math.isfinite(offset) or not 0 <= offset < part['duration']:
        raise ReadFailure('Libby returned an invalid listening position.')
    return part['start'] + round(offset)
