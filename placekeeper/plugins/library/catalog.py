"""OPDS 1 acquisition feed for CrossPoint."""
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from .storage import records
ATOM='http://www.w3.org/2005/Atom'
ET.register_namespace('',ATOM)

def feed(root):
    def add(parent,name,text=None,**attrs):
        element=ET.SubElement(parent,'{'+ATOM+'}'+name,attrs)
        element.text=text
        return element
    books=records(root)
    def updated(book):
        stamp=(Path(root)/'library'/book['id']/'metadata.json').stat().st_mtime
        return datetime.fromtimestamp(stamp,timezone.utc).isoformat().replace('+00:00','Z')
    node=ET.Element('{'+ATOM+'}feed')
    add(node,'id','urn:placekeeper:library');add(node,'title','Placekeeper')
    add(node,'updated',max((updated(book) for book in books),default='1970-01-01T00:00:00Z'))
    add(node,'link',href='/opds',rel='self',type='application/atom+xml;profile=opds-catalog;kind=acquisition')
    for book in books:
        entry=add(node,'entry');add(entry,'id','urn:sha256:'+book['sha256'])
        # CrossPoint ignores Content-Disposition and synthesizes the filename
        # from feed title/author. Omit author so every filename format agrees.
        add(entry,'title',book['filename'].removesuffix('.epub'));add(entry,'updated',updated(book))
        add(entry,'summary',book['title']+' — '+(book.get('author') or 'Unknown'))
        from urllib.parse import quote
        add(entry,'link',href='/epubs/'+book['id']+'/'+quote(book['filename']),rel='http://opds-spec.org/acquisition',type='application/epub+zip')
    return ET.tostring(node,encoding='unicode',xml_declaration=True)
