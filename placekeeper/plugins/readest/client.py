"""Readest reader pairing and GET-only cloud EPUB connector."""
import argparse
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit
from zipfile import ZipFile

import httpx
from placekeeper.storage import default_data_root, private_json

ORIGIN = 'https://web.readest.com'
AUTH = default_data_root()/'readest-device.json'
PENDING = default_data_root()/'readest-pairing.json'


def pairing_start(auth=None, pending=None):
    auth = Path(auth) if auth is not None else AUTH
    pending = Path(pending) if pending is not None else PENDING
    if auth.exists():
        raise ValueError('A Readest reader is already paired; revoke it before replacing credentials')
    with httpx.Client(timeout=20, follow_redirects=False) as c:
        response = c.post(ORIGIN+'/api/crosspoint/device/code')
        if response.status_code != 200:
            raise ValueError(f'Readest pairing returned HTTP {response.status_code}')
        data = response.json()
    verification = urlsplit(data['verification_uri_complete'])
    if verification.scheme != 'https' or verification.netloc != 'web.readest.com':
        raise ValueError('Unexpected pairing origin')
    private_json(pending, data)
    return {k:data[k] for k in ('user_code','verification_uri_complete','expires_in')}


def pairing_finish(auth=None, pending=None):
    auth = Path(auth) if auth is not None else AUTH
    pending = Path(pending) if pending is not None else PENDING
    data = json.loads(pending.read_text())
    with httpx.Client(timeout=20, follow_redirects=False) as c:
        r = c.post(ORIGIN+'/api/crosspoint/device/token', json={'device_code':data['device_code']})
    if r.status_code != 200:
        raise ValueError('Readest pairing is not approved or has expired')
    token = r.json()
    if not token.get('access_token'):
        raise ValueError('Readest returned no reader key')
    private_json(auth, token)
    pending.unlink(missing_ok=True)
    return {'paired':True,'credentials_saved_privately':True}


class ReadestSource:
    def __init__(self, auth=AUTH, client=None):
        self.key = json.loads(Path(auth).read_text())['access_token']
        self.client = client or httpx.Client(timeout=30, follow_redirects=False)

    def get(self, path, **kwargs):
        try:
            r = self.client.get(ORIGIN+path, headers={'Authorization':'Bearer '+self.key}, **kwargs)
        except httpx.HTTPError:
            raise ValueError('Readest connection failed') from None
        if r.status_code != 200:
            raise ValueError(f'Readest read failed: HTTP {r.status_code}')
        data=r.json()
        if not isinstance(data,dict):
            raise ValueError('Readest returned an unexpected response')
        return data

    def books(self):
        records = {}
        seen=set()
        for page in range(1,1001):
            items = self.get('/api/crosspoint/books',params={'page':page,'per_page':50})['items']
            if not isinstance(items,list) or any(not isinstance(i,dict) for i in items):
                raise ValueError('Readest returned an invalid catalog page')
            fingerprint=json.dumps(items,sort_keys=True)
            if fingerprint in seen:
                raise ValueError('Readest catalog pagination repeated a page')
            seen.add(fingerprint)
            for item in items[:50]:
                if not re.fullmatch(r'[a-f0-9]{28,32}', item.get('id','')):
                    raise ValueError('Readest returned an unsupported book hash')
                if not isinstance(item.get('title'),str):
                    raise ValueError('Readest returned an invalid title')
                records[item['id']] = item
            if len(items) <= 50:
                return list(records.values())
        raise ValueError('Readest catalog pagination exceeded limit')

    def download(self, book, directory):
        key = book['id']
        if not re.fullmatch(r'[a-f0-9]{28,32}',key):
            raise ValueError('Invalid Readest book hash')
        data = self.get('/api/crosspoint/books/'+key)
        url = data.get('downloadUrl','')
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port not in (None,443):
            raise ValueError('Readest returned an invalid cloud download URL')
        # Authenticated API supplies a short-lived object-store URL. Do not send
        # the reader key to it, follow redirects, or accept local destinations.
        import ipaddress
        host = parsed.hostname or ''
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise ValueError('IP literals are not cloud download hosts')
        if '.' not in host or host.endswith(('.local','.localhost','.internal')):
            raise ValueError('Local hosts are not cloud download hosts')
        directory = Path(directory);directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = directory/(key+'.epub')
        fd, tmp = tempfile.mkstemp(dir=directory,prefix=key+'-')
        try:
            size = 0
            with os.fdopen(fd,'wb') as f:
                try:
                    with self.client.stream('GET',url) as r:
                        if r.status_code != 200:
                            raise ValueError(f'Readest file download returned HTTP {r.status_code}')
                        for chunk in r.iter_bytes():
                            size += len(chunk)
                            if size > 64*1024*1024:
                                raise ValueError('EPUB exceeds 64 MiB limit')
                            f.write(chunk)
                except httpx.HTTPError:
                    raise ValueError('Readest file download failed; signed URL omitted') from None
            with ZipFile(tmp) as z:
                if z.read('mimetype').strip() != b'application/epub+zip':
                    raise ValueError('Readest file is not an EPUB')
                if sum(i.file_size for i in z.infolist()) > 256*1024*1024:
                    raise ValueError('Expanded EPUB exceeds limit')
            os.replace(tmp,path)
            return path
        finally:
            Path(tmp).unlink(missing_ok=True)

    def close(self):
        self.client.close()


if __name__ == '__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('command',choices=['pair','finish','list'])
    args=p.parse_args()
    if args.command=='pair': print(json.dumps(pairing_start()))
    elif args.command=='finish': print(json.dumps(pairing_finish()))
    else:
        source=ReadestSource()
        try:print(json.dumps(source.books(),indent=2))
        finally:source.close()
