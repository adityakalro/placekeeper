"""Incremental Kindle library catalog."""
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET

from audible import Authenticator
import httpx


class AmazonCatalog:
    def __init__(self, auth, client=None):
        self.auth = Authenticator.from_dict(json.loads(Path(auth).read_text()))
        self.client = client or httpx.Client(timeout=30, follow_redirects=False)

    def pull(self, previous):
        """Stage all pages before returning a new delta cursor/catalog."""
        # Old cursors skipped store books. Rebuild once to include older purchases.
        cursor = previous.get('cursor') if previous.get('includes_store_books') else None
        catalog = dict(previous.get('documents', {})) if cursor else {}
        seen = set()
        for page in range(100):
            params = {'item_count':1000}
            if cursor:
                params['last_sync_time'] = cursor
            try:
                r = self.client.get('https://todo-ta-g7g.amazon.com/FionaTodoListProxy/syncMetaData',
                                    params=params, auth=self.auth)
            except httpx.HTTPError:
                raise ValueError('Amazon library connection failed') from None
            if r.status_code != 200:
                raise ValueError(f'Amazon library returned HTTP {r.status_code}')
            root = ET.fromstring(r.content)
            for book in root.findall('./add_update_list/meta_data'):
                kind = book.findtext('cde_contenttype')
                if kind not in ('PDOC', 'EBOK'):
                    continue
                doc = book.findtext('ASIN','')
                if not re.fullmatch(r'[A-Z0-9]{10,64}',doc):
                    raise ValueError('Amazon returned an invalid document ID')
                catalog[doc] = {'document':doc, 'kind':kind, 'title':book.findtext('title',''),
                                'authors':[a.text for a in book.iter('author') if a.text]}
            removals = root.find('removal_list')
            if removals is not None:
                for asin in removals.iter('ASIN'):
                    catalog.pop(asin.text,None)
            next_cursor = root.findtext('sync_time')
            more = root.findtext('has_more','false').strip().lower() in ('true','1')
            if not next_cursor:
                raise ValueError('Amazon library returned no sync cursor')
            if not more:
                return {'cursor':next_cursor,'documents':catalog,'pages':page+1,'includes_store_books':True}
            if next_cursor == cursor or next_cursor in seen:
                raise ValueError('Amazon library pagination did not advance; checkpoint preserved')
            seen.add(next_cursor)
            cursor = next_cursor
        raise ValueError('Amazon library pagination exceeded limit; checkpoint preserved')

    def close(self):
        self.client.close()
