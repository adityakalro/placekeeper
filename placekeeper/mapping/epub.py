"""Read EPUB package paths and CrossPoint-compatible XHTML coordinates."""
import posixpath
from urllib.parse import unquote
from xml.etree import ElementTree as ET


def resolve(base, href):
    return posixpath.normpath(posixpath.join(posixpath.dirname(base), unquote(href)))


def package(archive):
    container = ET.fromstring(archive.read('META-INF/container.xml'))
    opf_path = container.find('.//{*}rootfile').attrib['full-path']
    opf = ET.fromstring(archive.read(opf_path))
    manifest = {item.attrib['id']: item for item in opf.findall('./{*}manifest/{*}item')}
    spine = opf.find('{*}spine')
    files = [resolve(opf_path, manifest[item.attrib['idref']].attrib['href'])
             for item in spine.findall('{*}itemref')]
    return opf_path, manifest, spine, files


def body(archive, filename):
    root = ET.fromstring(archive.read(filename))
    return next(element for element in root.iter() if element.tag.rsplit('}', 1)[-1] == 'body')


def children(element, xpath):
    counts = {}
    for child in element:
        tag = child.tag.rsplit('}', 1)[-1]
        counts[tag] = counts.get(tag, 0) + 1
        yield child, f'{xpath}/{tag}[{counts[tag]}]', tag


def text_runs(element, xpath):
    node = 0
    if element.text:
        node += 1
        yield f'{xpath}/text()[{node}]', element.text
    for child, child_path, tag in children(element, xpath):
        if tag not in ('style', 'script'):
            yield from text_runs(child, child_path)
        if child.tail:
            node += 1
            yield f'{xpath}/text()[{node}]', child.tail


def anchor_paths(element, xpath):
    if element.get('id'):
        yield element.attrib['id'], xpath
    for child, child_path, _ in children(element, xpath):
        yield from anchor_paths(child, child_path)
