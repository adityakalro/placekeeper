"""One-time local extraction using separately installed GPLv3 kfxlib.

Usage: python -m placekeeper.plugins.kindle.extract_kfx BOOK.azw8 OUTPUT_DIR --kfxlib-parent PATH
No credentials, network requests, or DRM processing are performed here.
"""
import argparse
import json
from pathlib import Path
import sys

from placekeeper.storage import file_sha256


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('book', type=Path)
    p.add_argument('output', type=Path)
    p.add_argument('--kfxlib-parent', required=True, type=Path)
    args = p.parse_args()
    sys.path[:0] = [str(args.kfxlib_parent), str(args.kfxlib_parent/'kfxlib/calibre-plugin-modules')]
    from kfxlib.yj_book import YJ_Book
    book = YJ_Book(str(args.book))
    book.decode_book()
    chunks = book.collect_content_position_info()
    locations = book.collect_location_map_info(chunks)
    if not locations:
        raise ValueError('Book has no original location table')
    args.output.mkdir(mode=0o700, parents=True, exist_ok=True)
    content = book.convert_to_json_content()
    def save(name, data):
        path = args.output/name
        # Create private from the outset, including when overwriting.
        import os
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'wb') as f:
            f.write(data)
        path.chmod(0o600)
    save('content.json', content if isinstance(content, bytes) else content.encode())
    save('locations.json', json.dumps([{'location': i+1, 'position': c.pid, 'length': c.length}
                                      for i, c in enumerate(locations)]).encode())
    save('source.json', json.dumps({'sha256': file_sha256(args.book),
                                   'location_count': len(locations), 'bridge_validated': False}).encode())
    print(json.dumps({'location_count': len(locations), 'output': str(args.output)}))


if __name__ == '__main__':
    main()
