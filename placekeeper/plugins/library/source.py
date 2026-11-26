"""EPUB sources shared by discovery adapters."""
from contextlib import ExitStack
from pathlib import Path
import shutil
from .storage import records

class LibrarySource:
    def __init__(self, root, readest_auth=None):
        self.root=Path(root)
        self.stack=ExitStack()
        self.remote=None
        auth=Path(readest_auth) if readest_auth else self.root/'readest-device.json'
        if auth.is_file():
            from placekeeper.plugins.readest.client import ReadestSource
            self.remote=ReadestSource(auth)
            self.stack.callback(self.remote.close)
    def books(self):
        local=records(self.root)
        # Prefer uploaded editions; Readest is an optional additional source.
        if self.remote:
            try:remote=self.remote.books()
            except (ValueError, OSError):
                if not local:raise
                remote=[]
        else:remote=[]
        titles={(b['title'].casefold(), b.get('author','').casefold()) for b in local}
        return local+[b for b in remote if (b['title'].casefold(),b.get('author','').casefold()) not in titles]
    def download(self, book, directory):
        if not book.get('local'):
            return self.remote.download(book,directory)
        directory=Path(directory);directory.mkdir(mode=0o700,parents=True,exist_ok=True)
        target=directory/(book['id']+'.epub')
        shutil.copyfile(self.root/'library'/book['id']/'book.epub',target)
        return target
    def close(self):
        self.stack.close()
