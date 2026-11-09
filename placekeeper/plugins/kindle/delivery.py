"""Read-only personal-document delivery probe using our approved device.

Writes private diagnostic files locally. Does not register a device or change
reading progress. Never prints credentials, manifest download URLs, or book text.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import time
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

import httpx
from audible import Authenticator

from placekeeper.storage import default_data_root, private_json


def probe(document, auth_path, output):
    if not re.fullmatch(r'[A-Z0-9]{10,64}', document):
        raise ValueError('Invalid document identifier')
    credentials = json.loads(Path(auth_path).read_text())
    info = credentials['device_info']
    auth = Authenticator.from_dict(credentials)
    headers = {
        'x-adp-reason': 'ArchivedItems', 'x-adp-transport': 'WiFi',
        'x-adp-attemptcount': '1', 'x-adp-sw': '1191420554',
        'accept-language': 'en-US',
        'x-amzn-accept-type': 'application/x.amzn.digital.deliverymanifest@1.0',
        'x-adp-correlationid': f'Device:{info["device_type"]}:{info["device_serial_number"]};kindle.PDOC:{document}:{int(time.time()*1000)}',
    }
    output = Path(output)
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    with httpx.Client(auth=auth, timeout=25, follow_redirects=False) as client:
        response = client.get('https://kindle-digital-delivery.amazon.com/delivery/manifest/kindle.pdoc/'+document,
                              headers=headers)
    raw = output/(document+'-manifest.bin')
    import os
    with os.fdopen(os.open(raw, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'wb') as f:
        f.write(response.content)
    report = {'document': document, 'device_type': info['device_type'],
              'http_status': response.status_code,
              'content_type': response.headers.get('content-type'), 'bytes': len(response.content),
              'sha256': hashlib.sha256(response.content).hexdigest()}
    try:
        manifest = response.json()
    except ValueError:
        report['response_format'] = 'non-JSON'
        if b'CloudFront' in response.content:
            report['edge_error'] = 'CloudFront rejected request'
    else:
        report['response_format'] = 'JSON'
        report['top_level_fields'] = list(manifest) if isinstance(manifest, dict) else []
        if isinstance(manifest, dict):
            report['resource_types'] = [r.get('type') for r in manifest.get('resources', [])]
            # The full manifest stays private, including signed content URLs.
            private_json(output/(document+'-manifest.json'), manifest)
    private_json(output/(document+'-report.json'), report)
    return report


def discover_documents(auth_path, output):
    """Read one library page; retain only personal-document pairing metadata."""
    credentials = json.loads(Path(auth_path).read_text())
    auth = Authenticator.from_dict(credentials)
    with httpx.Client(auth=auth, timeout=30, follow_redirects=False) as client:
        response = client.get('https://todo-ta-g7g.amazon.com/FionaTodoListProxy/syncMetaData',
                              params={'item_count': 1000})
    if response.status_code != 200:
        raise ValueError(f'Library discovery returned HTTP {response.status_code}')
    root = ET.fromstring(response.content)
    records = list(root.iter('meta_data'))
    documents = [{'document': b.findtext('ASIN'), 'title': b.findtext('title'),
                  'authors': [a.text for a in b.iter('author') if a.text]}
                 for b in records if b.findtext('cde_contenttype') == 'PDOC']
    report = {'http_status': 200, 'total_items_on_page': len(records),
              'personal_documents': len(documents), 'has_more': root.findtext('has_more'),
              'sync_time': root.findtext('sync_time'), 'progress_writes': 0, 'mac_app_used': False}
    output = Path(output)
    private_json(output/'personal-documents.json', {'documents': documents, 'discovery': report})
    private_json(output/'library-discovery-report.json', report)
    return report


def download_kfx(document, manifest, output, client=None):
    """Fetch only the processed text resource; no annotations or DRM resources."""
    import os
    import tempfile
    if manifest.get('content', {}).get('id') != document:
        raise ValueError('Manifest does not identify the requested document')
    resources = [r for r in manifest.get('resources', []) if r.get('type') == 'KINDLE_MAIN_BASE']
    if len(resources) != 1:
        raise ValueError('Manifest does not have one processed KFX content resource')
    resource = resources[0]
    url = resource.get('optimalEndpoint', {}).get('directUrl', '')
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or parsed.hostname != 'kindle-digital-delivery.amazon.com':
        raise ValueError('Manifest has no supported Amazon direct content URL')
    output = Path(output)
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    owned = client is None
    client = client or httpx.Client(timeout=60, follow_redirects=False)
    fd, temp = tempfile.mkstemp(prefix=document+'-', dir=output)
    path = output/(document+'.azw8')
    try:
        size, head, checksum = 0, bytearray(), hashlib.sha256()
        try:
            with os.fdopen(fd, 'wb') as f, client.stream('GET', url) as response:
                if response.status_code != 200:
                    raise ValueError(f'Content download returned HTTP {response.status_code}')
                content_type = response.headers.get('content-type')
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > 64*1024*1024:
                        raise ValueError('Content exceeds the 64 MiB probe limit')
                    if len(head) < 4:
                        head.extend(chunk[:4-len(head)])
                    checksum.update(chunk)
                    f.write(chunk)
        except httpx.HTTPError:
            raise ValueError('Amazon content connection failed; signed URL omitted') from None
        if bytes(head) != b'CONT':
            raise ValueError('Downloaded response is not a KFX container')
        expected_size = resource.get('size')
        if isinstance(expected_size, int) and size != expected_size:
            raise ValueError('Downloaded size differs from the manifest')
        os.replace(temp, path)
        report = {'document': document, 'downloaded': True, 'bytes': size,
                  'content_type': content_type, 'sha256': checksum.hexdigest(),
                  'is_kfx': True, 'path': str(path), 'mac_app_used': False}
        private_json(output/(document+'-download-report.json'), report)
        return report
    finally:
        Path(temp).unlink(missing_ok=True)
        if owned:
            client.close()


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument('--document')
    group.add_argument('--list', action='store_true', help='Discover personal documents without downloading them')
    p.add_argument('--auth', type=Path, default=default_data_root()/'amazon-device.json')
    p.add_argument('--output', type=Path, default=default_data_root()/'delivery-probes')
    p.add_argument('--download', action='store_true', help='Download the manifest KFX resource if available')
    args = p.parse_args()
    if args.list:
        if args.download:
            p.error('--download requires --document')
        print(json.dumps(discover_documents(args.auth, args.output), indent=2))
        raise SystemExit(0)
    result = probe(args.document, args.auth, args.output)
    print(json.dumps(result, indent=2))
    if args.download:
        if result['http_status'] != 200:
            raise SystemExit('Manifest failed; not downloading')
        manifest = json.loads((args.output/(args.document+'-manifest.json')).read_text())
        print(json.dumps(download_kfx(args.document, manifest, args.output), indent=2))
