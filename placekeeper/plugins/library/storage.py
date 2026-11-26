"""Bounded EPUB validation and private, atomic library storage."""
from hashlib import sha256
from pathlib import Path
import os
import re
import tempfile
import xml.etree.ElementTree as ET
from zipfile import ZipFile, BadZipFile
from placekeeper.storage import private_json
import json

MAX_UPLOAD=64*1024*1024
MAX_EXPANDED=256*1024*1024

def record(root,key):
    # Validate the identity before constructing paths; one-book operations must
    # not scan or deserialize every metadata file in the library.
    book_path(root,key)
    return json.loads((Path(root)/'library'/key/'metadata.json').read_text())


def records(root):
    directory=Path(root)/'library'
    if not directory.exists():return []
    return [json.loads(p.read_text()) for p in sorted(directory.glob('*/metadata.json'))
            if re.fullmatch('[a-f0-9]{32}',p.parent.name)]

def save(root, stream, size):
    if not 0<size<=MAX_UPLOAD:raise ValueError('EPUBs must be no larger than 64 MiB.')
    directory=Path(root)/'library';directory.mkdir(mode=0o700,parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.upload-',dir=directory) as temporary:
        path=Path(temporary)/'book.epub'
        remaining=size;fingerprint=sha256()
        with path.open('wb') as output:
            os.chmod(path,0o600)
            while remaining:
                block=stream.read(min(65536,remaining))
                if not block:raise ValueError('Upload was interrupted.')
                remaining-=len(block);fingerprint.update(block);output.write(block)
        try:
            with ZipFile(path) as archive:
                entries=archive.infolist()
                if len({e.filename for e in entries})!=len(entries):raise ValueError('EPUB contains duplicate entries.')
                if len(entries)>10000 or sum(e.file_size for e in entries)>MAX_EXPANDED:
                    raise ValueError('EPUB contents exceed the library limits.')
                def xml(name):
                    info=archive.getinfo(name)
                    if info.file_size>2*1024*1024:raise ValueError('EPUB metadata is too large.')
                    raw=archive.read(name)
                    if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():raise ValueError('Unsupported EPUB metadata.')
                    return ET.fromstring(raw)
                if archive.getinfo('mimetype').file_size>100:raise ValueError('Invalid EPUB type.')
                if archive.read('mimetype')!=b'application/epub+zip':raise ValueError('Choose a valid EPUB file.')
                container=xml('META-INF/container.xml')
                package=next(e.attrib['full-path'] for e in container.iter() if e.tag.endswith('}rootfile'))
                metadata=xml(package)
                def field(name):
                    return next((''.join(e.itertext()).strip() for e in metadata.iter() if e.tag=='{http://purl.org/dc/elements/1.1/}'+name),'')
                title=field('title');author=field('creator')
                if not title or len(title)>500:raise ValueError('EPUB needs a valid title.')
        except (BadZipFile,KeyError,StopIteration,ET.ParseError,RuntimeError,NotImplementedError):
            raise ValueError('Choose a valid, unencrypted EPUB file.') from None
        key=fingerprint.hexdigest()[:32]
        target=directory/key
        if target.exists():return json.loads((target/'metadata.json').read_text())
        # A predictable safe filename is also the CrossPoint sync identity.
        filename=(re.sub(r'[^a-zA-Z0-9 .()-]','_',title).strip(' .')[:80] or 'Book')+'-'+key[:8]+'.epub'
        book={'id':key,'title':title,'author':author,'filename':filename,'local':True,'sha256':fingerprint.hexdigest()}
        private_json(Path(temporary)/'metadata.json',book)
        os.rename(temporary,target)
        return book

def book_path(root,key):
    if not isinstance(key,str) or not re.fullmatch('[a-f0-9]{32}',key):raise ValueError('Unknown EPUB.')
    return Path(root)/'library'/key/'book.epub'
