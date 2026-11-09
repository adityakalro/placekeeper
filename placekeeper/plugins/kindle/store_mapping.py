"""Extract chapter positions from an entitled Kindle Store delivery."""
from bisect import bisect_right
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit

from audible import Authenticator
import httpx

from placekeeper.mapping.chapters import chapter_key, epub_entries
from placekeeper.mapping.text import epub_index

HOST = 'kindle-digital-delivery.amazon.com'
MAX_RESOURCE = 32 * 1024 * 1024


class StoreDeliveryDenied(ValueError):
    """Amazon refused delivery of this edition to the registered device."""


def fetch_resource(client, url, *, auth=None, headers=None):
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or parsed.netloc != HOST:
        raise ValueError('Unexpected Kindle delivery host')
    data = bytearray()
    with client.stream('GET', url, auth=auth, headers=headers) as response:
        if response.status_code != 200:
            raise ValueError('Kindle delivery resource unavailable')
        for chunk in response.iter_bytes():
            data.extend(chunk)
            if len(data) > MAX_RESOURCE:
                raise ValueError('Kindle delivery resource is too large')
    return bytes(data)


def delivery(document, auth_path):
    credentials = json.loads(Path(auth_path).read_text())
    device = credentials['device_info']
    serial = device['device_serial_number']
    auth = Authenticator.from_dict(credentials)
    headers = {
        'x-adp-reason': 'ArchivedItems', 'x-adp-transport': 'WiFi',
        'x-adp-attemptcount': '1', 'x-adp-sw': '1191420554', 'accept-language': 'en-US',
        'x-adp-correlationid': f'Device:{device["device_type"]}:{serial};kindle.EBOK:{document}:{int(time.time()*1000)}',
    }
    with httpx.Client(timeout=30, follow_redirects=False) as client:
        response = client.get(f'https://{HOST}/delivery/manifest/kindle.ebook/{document}', auth=auth,
                              headers={**headers, 'x-amzn-accept-type': 'application/x.amzn.digital.deliverymanifest@1.0'})
        if response.status_code == 403:
            raise StoreDeliveryDenied('Kindle Store delivery was denied')
        if response.status_code != 200:
            raise ValueError(f'Kindle Store manifest returned HTTP {response.status_code}')
        manifest = response.json()
        if manifest['content']['id'] != document:
            raise ValueError('Kindle Store manifest identifies another edition')
        resources = {}
        for kind in ('KINDLE_MAIN_BASE', 'KINDLE_MAIN_METADATA', 'DRM_VOUCHER'):
            matches = [item for item in manifest['resources'] if item['type'] == kind]
            if len(matches) != 1:
                raise ValueError('Unsupported Kindle Store delivery layout')
            resources[kind] = matches[0]
        voucher = fetch_resource(client, resources['DRM_VOUCHER']['endpoint']['url'], auth=auth,
                                 headers={**headers, 'Accept': 'application/x-com.amazon.drm.Voucher@1.0'})
        with ThreadPoolExecutor(max_workers=2) as pool:
            base = pool.submit(fetch_resource, client, resources['KINDLE_MAIN_BASE']['optimalEndpoint']['directUrl'])
            metadata = fetch_resource(client, resources['KINDLE_MAIN_METADATA']['optimalEndpoint']['directUrl'])
            return serial, voucher, base.result(), metadata



def extract_chapters(document, auth_path, parser_path):
    serial, voucher_data, base, metadata = delivery(document, auth_path)
    paths = [str(parser_path), str(Path(parser_path)/'kfxlib/calibre-plugin-modules')]
    sys.path[:0] = paths
    phase = 'loading parser'
    try:
        from dedrm.drmion import decrypt
        from dedrm.ion import DrmIonVoucher
        from kfxlib.ion import unannotated
        from kfxlib.kfx_container import KfxContainer
        from kfxlib.utilities import DataFile
        from kfxlib.yj_book import YJ_Book

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            phase = 'reading voucher'
            voucher = DrmIonVoucher(io.BytesIO(voucher_data), serial, '')
            voucher.parse()
            if set(voucher.lockparams) - {'CLIENT_ID'}:
                raise ValueError('Unsupported Kindle voucher lock parameters')
            phase = 'unlocking voucher'
            voucher.decryptvoucher()
            phase = 'decoding KFX'
            book = YJ_Book('store.kfx')
            for data in (decrypt(base, voucher.secretkey), metadata):
                container = KfxContainer(book.symtab, DataFile('store.kfx', data))
                container.deserialize()
                book.fragments.extend(container.get_fragments())
            phase = 'reading position table'
            positions = book.collect_position_map_info()
            locations = [entry.pid for entry in book.collect_location_map_info(positions)]
            if not locations or locations != sorted(locations):
                raise ValueError('Kindle Store location table is incomplete')
            phase = 'reading chapter table'
            toc = []
            for fragment in book.fragments.get_all('$389'):
                for order in fragment.value:
                    for nav in map(unannotated, order.get('$392', [])):
                        if isinstance(nav, dict) and nav.get('$235') == '$212':
                            for entry in map(unannotated, nav['$247']):
                                label = unannotated(entry['$241']).get('$244', '')
                                pointer = unannotated(entry['$246'])
                                position = book.pid_for_eid(pointer['$155'], pointer.get('$143', 0), positions)
                                if label and position is not None:
                                    toc.append([label, bisect_right(locations, position)])
            if not toc:
                raise ValueError('Kindle Store chapter table is unavailable')
            return {'locations': len(locations), 'toc': toc}
    except Exception as error:
        raise ValueError(f'Kindle Store extraction failed while {phase} ({type(error).__name__})') from None
    finally:
        del sys.path[:len(paths)]


class StoreMapper:
    """Map within matching numbered chapters using Kindle location buckets."""

    def __init__(self, epub, table):
        self.epub = epub_index(epub)
        ebook = [(chapter_key(label), anchor) for label, anchor in epub_entries(epub, self.epub.coordinates)]
        kindle = [(chapter_key(label), location) for label, location in table['toc']]
        if any(type(location) is not int or not 1 <= location <= table['locations'] for _, location in kindle):
            raise ValueError('Kindle chapter location is outside the book')
        ebook_numbered = [key for key, _ in ebook if key.startswith('chapter ')]
        kindle_numbered = [key for key, _ in kindle if key.startswith('chapter ')]
        if len(ebook_numbered) < 2 or ebook_numbered != kindle_numbered or len(set(ebook_numbered)) != len(ebook_numbered):
            raise ValueError('Kindle Store and EPUB numbered chapters differ')
        self.rows = []
        for entries, limit in ((ebook, len(self.epub.coordinates)), (kindle, table['locations']+1)):
            ranges = {}
            for index, (key, start) in enumerate(entries):
                if not key.startswith('chapter '):
                    continue
                end = next((value for _, value in entries[index+1:] if value > start), limit)
                if not 0 <= start < end <= limit or (ranges and start < next(reversed(ranges.values()))[1]):
                    raise ValueError('Invalid chapter boundaries')
                ranges[key] = (start, end)
            self.rows.append(ranges)

    def convert(self, value, reverse=False):
        source, destination = self.rows[::-1] if reverse else self.rows
        for key, (start, end) in source.items():
            if start <= value < end:
                first, last = destination[key]
                return first + min(last-first-1, (value-start)*(last-first)//(end-start))
        raise ValueError('Position is outside the matched chapters')

    def from_sidecar(self, position):
        location = position//150+1
        return {'xpointer': self.epub.coordinates[self.convert(location, reverse=True)]}

    def from_xpointer(self, xpointer):
        location = self.convert(self.epub.coordinates.at_or_after(xpointer))
        low = (location-1)*150
        return {'sidecar_range': [low, low+149]}
