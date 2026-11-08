"""Approximate ebook/audio handoffs using matched chapter boundaries."""
import bisect
import posixpath
import re
import unicodedata
from urllib.parse import unquote
from xml.etree import ElementTree as ET
from zipfile import ZipFile


def chapter_key(label):
    label = unicodedata.normalize('NFKC', label).casefold().strip()
    if label.isdecimal():
        return 'chapter ' + str(int(label))
    match = re.search(r'\bchapter\s+([\w-]+(?:\s+(?:one|two|three|four|five|six|seven|eight|nine))?)', label)
    if match:
        value = match[1].replace('-', ' ')
        small = 'zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen'.split()
        tens = {'twenty': 20, 'thirty': 30, 'forty': 40, 'fifty': 50,
                'sixty': 60, 'seventy': 70, 'eighty': 80, 'ninety': 90}
        words = value.split()
        if value.isdecimal():
            return 'chapter ' + str(int(value))
        if value in small:
            return 'chapter ' + str(small.index(value))
        if words[0] in tens and len(words) <= 2:
            return 'chapter ' + str(tens[words[0]] + (small.index(words[1]) if len(words) == 2 else 0))
    return re.sub(r'[^\w]+', '', label)


def epub_entries(epub, coordinates):
    with ZipFile(epub) as book:
        opf_path = ET.fromstring(book.read('META-INF/container.xml')).find('.//{*}rootfile').get('full-path')
        opf = ET.fromstring(book.read(opf_path))
        manifest = {item.get('id'): item for item in opf.findall('.//{*}manifest/{*}item')}
        spine = opf.find('{*}spine')
        def resolve(base, href):
            return posixpath.normpath(posixpath.join(posixpath.dirname(base), unquote(href)))
        files = [resolve(opf_path, manifest[item.get('idref')].get('href'))
                 for item in spine.findall('{*}itemref')]
        nav = next((item for item in manifest.values() if 'nav' in item.get('properties', '').split()), None)
        toc_item = nav or manifest.get(spine.get('toc'))
        if toc_item is None:
            raise ValueError('EPUB has no supported table of contents')
        toc_path = resolve(opf_path, toc_item.get('href'))
        toc = ET.fromstring(book.read(toc_path))
        if nav is not None:
            navigation = next(n for n in toc.iter() if n.tag.rsplit('}', 1)[-1] == 'nav'
                and 'toc' in n.get('{http://www.idpf.org/2007/ops}type', '').split())
            links = [(''.join(a.itertext()).strip(), a.get('href')) for a in navigation.findall('.//{*}a')]
        else:
            links = [(n.find('{*}navLabel/{*}text').text.strip(), n.find('{*}content').get('src'))
                     for n in toc.findall('.//{*}navPoint')]
        prefix_indices = list(zip(coordinates.prefixes, coordinates.starts))
        entries = []
        for label, href in links:
            path, _, anchor = href.partition('#')
            target_file = resolve(toc_path, path)
            if target_file not in files:
                continue
            fragment = files.index(target_file) + 1
            prefix = f'/body/DocFragment[{fragment}]/body'
            if anchor:
                body = ET.fromstring(book.read(target_file)).find('.//{*}body')
                found = []
                def walk(element, xpath):
                    if element.get('id') == unquote(anchor):
                        found.append(xpath)
                    counts = {}
                    for child in element:
                        tag = child.tag.rsplit('}', 1)[-1]
                        counts[tag] = counts.get(tag, 0) + 1
                        walk(child, f'{xpath}/{tag}[{counts[tag]}]')
                walk(body, prefix)
                if len(found) != 1:
                    raise ValueError('EPUB chapter anchor is ambiguous')
                prefix = found[0]
            start = next((index for node, index in prefix_indices
                          if node.startswith(prefix + '/') or node == prefix), None)
            if start is None:
                if chapter_key(label).startswith('chapter '):
                    raise ValueError('EPUB chapter has no readable text')
                continue
            entries.append((label, start))
    starts = [start for _, start in entries]
    if starts != sorted(starts):
        raise ValueError('EPUB table of contents is not in reading order')
    return entries


class ChapterMap:
    def __init__(self, epub, coordinates, timing):
        entries = epub_entries(epub, coordinates)
        ebook = {}
        for i, (label, start) in enumerate(entries):
            end = next((pos for _, pos in entries[i+1:] if pos > start), len(coordinates))
            key = chapter_key(label)
            if key in ebook:
                raise ValueError('EPUB contains duplicate chapter labels')
            ebook[key] = (label, start, end)
        audio_entries = []
        for label, start in timing['chapters']:
            # Libby repeats a chapter label when that chapter continues in a new file.
            continuation = re.search(r'\s+\(\d+:\d+(?::\d+)?\)$', label)
            if continuation:
                base = label[:continuation.start()]
                if audio_entries and chapter_key(base) == chapter_key(audio_entries[-1][0]):
                    continue
                raise ValueError('Audio continuation has no matching chapter')
            audio_entries.append((label, start))
        audio = {}
        for i, (label, start) in enumerate(audio_entries):
            key = chapter_key(label)
            end = audio_entries[i+1][1] if i+1 < len(audio_entries) else timing['duration']
            if key in audio or start >= end:
                raise ValueError('Audio chapter boundaries are ambiguous')
            audio[key] = (label, start, end)
        ebook_numbered = {key for key in ebook if key.startswith('chapter ')}
        audio_numbered = {key for key in audio if key.startswith('chapter ')}
        if ebook_numbered != audio_numbered:
            raise ValueError('EPUB and audiobook chapter sequences differ')
        keys = [key for key in ebook if key in audio]
        if len(keys) < 2:
            raise ValueError('Too few matching chapters')
        if keys != [key for key in audio if key in ebook]:
            raise ValueError('EPUB and audiobook chapters differ in order')
        self.rows = [(ebook[key][0], ebook[key][1], ebook[key][2],
                      audio[key][1], audio[key][2]) for key in keys]
        self.coordinates = coordinates

    def to_audio(self, xpointer):
        index = self.coordinates.at_or_after(xpointer)
        for label, first, last, start, end in self.rows:
            if first <= index < last:
                target = start + min(end-start-1, round((index-first)/(last-first)*(end-start)))
                return target, label
        raise ValueError('Ebook position is outside the matched chapters')

    def to_text(self, milliseconds):
        for label, first, last, start, end in self.rows:
            if start <= milliseconds < end:
                index = first + min(last-first-1, int((milliseconds-start)/(end-start)*(last-first)))
                return self.coordinates[index], label
        raise ValueError('Audio position is outside the matched chapters')
