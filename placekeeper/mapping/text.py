"""Offline EPUB/KFX text matching. No network access or progress writes.

Amazon's legacy sidecar coordinates are NOT KFX character positions. The
location bridge below is experimental and must be validated for each document.
"""
from __future__ import annotations

import argparse
from array import array
from bisect import bisect_left, bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import posixpath
import unicodedata
import xml.etree.ElementTree as ET
from zipfile import ZipFile


def normalized(text):
    # Keep a source coordinate for every normalized character, including NFKC
    # expansions. Ignoring whitespace handles inline tags and line wrapping.
    for index, char in enumerate(text):
        for c in unicodedata.normalize('NFKC', char):
            c = {'’': "'", '‘': "'", '“': '"', '”': '"', '\u00ad': ''}.get(c, c)
            if c and not c.isspace():
                yield c, index


@dataclass
class TextIndex:
    text: str
    coordinates: Sequence

    @classmethod
    def make(cls, runs):
        chars, coordinates = [], Coordinates()
        for text, coordinate in runs:
            first=coordinate(0)
            start=len(coordinates)
            string=isinstance(first,str)
            if string:
                prefix,zero=first.rsplit('.',1)
                if zero!='0' or coordinate(1)!=prefix+'.1':
                    raise ValueError('Unsupported text-node coordinate function')
            if coordinates.string is not None and coordinates.string!=string:
                raise ValueError('Mixed coordinate domains')
            coordinates.string=string
            for char, offset in normalized(text):
                chars.append(char)
                coordinates.values.append(offset if string else coordinate(offset))
            if string and len(coordinates)>start:
                coordinates.starts.append(start)
                coordinates.prefixes.append(prefix)
                coordinates.nodes[prefix]=(start,len(coordinates))
        return cls(''.join(chars), coordinates)

    def unique_match(self, other, index):
        # Require substantial context; grow in both directions for repeated
        # passages. Never silently select the first occurrence.
        for radius in (100, 250, 750, 2000):
            start, end = max(0, index-radius), min(len(self.text), index+radius)
            needle = self.text[start:end]
            if len(needle) < 40:
                continue
            found = other.text.find(needle)
            if found >= 0 and other.text.find(needle, found+1) < 0:
                return found + index-start
        raise ValueError('No unique exact text context; mapping needs review')


class Coordinates(Sequence):
    """Compact integers plus one XPath prefix per text node.

    Avoid retaining millions of Python strings on the Pi Zero. XPath lookup is
    a node lookup and bounded binary search, rather than a full-book scan.
    """
    def __init__(self):
        self.values=array('Q')
        self.starts=[]
        self.prefixes=[]
        self.nodes={}
        self.string=None

    def __len__(self):return len(self.values)

    def __getitem__(self,index):
        if isinstance(index,slice):
            return [self[i] for i in range(*index.indices(len(self)))]
        if index<0:index+=len(self)
        if not 0<=index<len(self):raise IndexError('Coordinate index out of range')
        value=self.values[index]
        if not self.string:return value
        node=bisect_right(self.starts,index)-1
        return f'{self.prefixes[node]}.{value}'

    def __eq__(self,other):
        return isinstance(other,Sequence) and len(self)==len(other) and all(a==b for a,b in zip(self,other))

    def index(self,value,start=0,stop=None):
        stop=len(self) if stop is None else min(stop,len(self))
        if self.string:
            try:
                prefix,offset=value.rsplit('.',1)
                if int(offset)<0:raise ValueError('Negative text offset')
                low,high=self.nodes[prefix]
                index=bisect_left(self.values,int(offset),max(start,low),min(stop,high))
                if index<min(stop,high) and self.values[index]==int(offset):return index
            except (ValueError,KeyError,AttributeError):pass
            raise ValueError('Coordinate is absent')
        return self.values.index(value,start,stop)

    def at_or_after(self,value):
        prefix,offset=value.rsplit('.',1)
        if int(offset)<0:raise ValueError('Negative text offset')
        if prefix not in self.nodes:raise ValueError('XPointer text node is absent')
        low,high=self.nodes[prefix]
        index=bisect_left(self.values,int(offset),low,high)
        if index>=high:raise ValueError('XPointer offset exceeds text node')
        return index

    def __contains__(self,value):
        try:self.index(value);return True
        except ValueError:return False


def epub_index(path):
    runs = []
    with ZipFile(path) as z:
        container = ET.fromstring(z.read('META-INF/container.xml'))
        opf_path = next(e.attrib['full-path'] for e in container.iter()
                        if e.tag.rsplit('}', 1)[-1] == 'rootfile')
        opf = ET.fromstring(z.read(opf_path))
        manifest = {e.attrib['id']: e.attrib['href'] for e in opf.iter()
                    if e.tag.rsplit('}', 1)[-1] == 'item'}
        spine = [e.attrib['idref'] for e in opf.iter()
                 if e.tag.rsplit('}', 1)[-1] == 'itemref']
        for fragment, idref in enumerate(spine, 1):
            href = posixpath.normpath(posixpath.join(posixpath.dirname(opf_path), manifest[idref]))
            root = ET.fromstring(z.read(href))
            body = next(e for e in root.iter() if e.tag.rsplit('}', 1)[-1] == 'body')

            def walk(element, xpath):
                node = 0
                if element.text:
                    node += 1
                    base = f'{xpath}/text()[{node}]'
                    runs.append((element.text, lambda o, b=base: f'{b}.{o}'))
                counts = {}
                for child in element:
                    tag = child.tag.rsplit('}', 1)[-1]
                    counts[tag] = counts.get(tag, 0)+1
                    if tag not in ('script', 'style'):
                        walk(child, f'{xpath}/{tag}[{counts[tag]}]')
                    if child.tail:
                        node += 1
                        base = f'{xpath}/text()[{node}]'
                        runs.append((child.tail, lambda o, b=base: f'{b}.{o}'))
            walk(body, f'/body/DocFragment[{fragment}]/body')
    return TextIndex.make(runs)


def kfx_index(path):
    data = json.loads(Path(path).read_text())['data']
    positions = [r['position'] for r in data if r['type'] == 1]
    if positions != sorted(positions):
        raise ValueError('KFX content is not in monotonic reading order')
    return TextIndex.make((r['content'], lambda o, p=r['position']: p+o)
                          for r in data if r['type'] == 1)


def epub_position_details(path, xpointer):
    """Protocol fallback: spine byte weight times visible chapter text fraction.

The XPath remains authoritative. This is a layout-independent estimate for
firmware that has not built a pagination cache for the destination chapter.
"""
    import re
    match = re.fullmatch(r'/body/DocFragment\[(\d+)\]/body(.*)/text\(\)\[(\d+)\]\.(\d+)', xpointer)
    if not match:
        raise ValueError('Unsupported EPUB XPointer')
    fragment, ancestry, node, offset = match.groups()
    spine_index, node, offset = int(fragment)-1, int(node), int(offset)
    with ZipFile(path) as z:
        container = ET.fromstring(z.read('META-INF/container.xml'))
        opf_path = next(e.attrib['full-path'] for e in container.iter()
                        if e.tag.rsplit('}', 1)[-1] == 'rootfile')
        opf = ET.fromstring(z.read(opf_path))
        manifest = {e.attrib['id']: e.attrib['href'] for e in opf.iter()
                    if e.tag.rsplit('}', 1)[-1] == 'item'}
        spine = [posixpath.normpath(posixpath.join(posixpath.dirname(opf_path), manifest[e.attrib['idref']]))
                 for e in opf.iter() if e.tag.rsplit('}', 1)[-1] == 'itemref']
        if not 0 <= spine_index < len(spine):
            raise ValueError('Spine index is absent')
        sizes = [z.getinfo(href).file_size for href in spine]
        root = ET.fromstring(z.read(spine[spine_index]))
        body = next(e for e in root.iter() if e.tag.rsplit('}', 1)[-1] == 'body')
    runs = []

    def walk(element, xpath):
        n = 0
        if element.text:
            n += 1
            runs.append((f'{xpath}/text()[{n}]', element.text))
        counts = {}
        for child in element:
            tag = child.tag.rsplit('}', 1)[-1]
            counts[tag] = counts.get(tag, 0)+1
            if tag not in ('style', 'script'):
                walk(child, f'{xpath}/{tag}[{counts[tag]}]')
            if child.tail:
                n += 1
                runs.append((f'{xpath}/text()[{n}]', child.tail))
    walk(body, '/body')
    target = f'/body{ancestry}/text()[{node}]'
    before, found = 0, None
    for xpath, text in runs:
        if xpath == target:
            if offset > len(text):
                raise ValueError('Text offset exceeds node length')
            found = before+offset
        before += len(text)
    if found is None or not before or not sum(sizes):
        raise ValueError('No visible text at the requested coordinate')
    fraction = found/before
    return {'spine': spine_index, 'visible_text_offset': found,
            'chapter_text_fraction': fraction,
            'percentage': (sum(sizes[:spine_index])+sizes[spine_index]*fraction)/sum(sizes),
            'percentage_method': 'EPUB spine XHTML byte weights and chapter visible-text fraction; fallback only'}


class Mapper:
    def __init__(self, epub, content, locations, *, epub_text=None, kfx_text=None):
        self.epub = epub_index(epub) if epub_text is None else epub_text
        self.kfx = kfx_index(content) if kfx_text is None else kfx_text
        self.locations = json.loads(Path(locations).read_text())
        self.starts = [r['position'] for r in self.locations]
        if self.starts != sorted(self.starts):
            raise ValueError('Location positions must be monotonic')
        if [r['location'] for r in self.locations] != list(range(1, len(self.locations)+1)):
            raise ValueError('Location table has missing location numbers')

    def from_xpointer(self, xpointer):
        try:
            index = self.epub.coordinates.index(xpointer)
        except ValueError:
            # Whitespace may have been removed. Snap forward within this text
            # node only, never silently switch to a different paragraph.
            index=self.epub.coordinates.at_or_after(xpointer)
        matched = self.epub.unique_match(self.kfx, index)
        pid = self.kfx.coordinates[matched]
        location = bisect_right(self.starts, pid)
        if not location:
            raise ValueError('Position precedes location table')
        lower = (location-1)*150
        return {'xpointer': xpointer, 'kfx_position': pid, 'kindle_location': location,
                'sidecar_range': [lower, lower+149],
                'suggested_forward_position': lower+149,
                'precision': 'location bucket; legacy sidecar bridge requires document validation'}

    def from_sidecar(self, position):
        if position < 0:
            raise ValueError('Negative position')
        location = position//150+1
        if location > len(self.locations):
            raise ValueError('Position exceeds location table')
        # A legacy location bucket does not expose a precise intra-location
        # offset. Select its first real text character, avoiding false precision.
        pid = self.starts[location-1]
        index = bisect_right(self.kfx.coordinates, pid-1)
        if index >= len(self.kfx.coordinates):
            raise ValueError('No text at this location')
        matched = self.kfx.unique_match(self.epub, index)
        return {'sidecar_position': position, 'kindle_location': location,
                'xpointer': self.epub.coordinates[matched],
                'precision': 'start of location bucket'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--epub', required=True, type=Path)
    parser.add_argument('--content', required=True, type=Path)
    parser.add_argument('--locations', required=True, type=Path)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--xpointer')
    group.add_argument('--sidecar', type=int)
    args = parser.parse_args()
    mapper = Mapper(args.epub, args.content, args.locations)
    result = mapper.from_xpointer(args.xpointer) if args.xpointer else mapper.from_sidecar(args.sidecar)
    result['fingerprints'] = {name: hashlib.sha256(path.read_bytes()).hexdigest()
                              for name, path in [('epub', args.epub), ('content', args.content), ('locations', args.locations)]}
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
