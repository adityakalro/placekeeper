"""Contracts for trusted, locally installed Placekeeper plugins."""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

Native = int | str


@dataclass
class Book:
    """One verified edition. Anchors are ordered offsets within this edition."""
    key: str
    title: str
    edition: str
    bindings: dict = field(default_factory=dict)
    epub: Path | None = None
    mapper: object | None = None
    initial_state: Callable | None = None


@dataclass(frozen=True)
class Observation:
    native: Native
    anchor: int
    stamp: float | None = None
    precision_seconds: float = 0
    own: bool = False


class Reader(Protocol):
    """An open, per-book progress session in a shared coordinate space."""
    prefer_recent: bool

    def read(self, hint: dict) -> Observation: ...
    def equivalent(self, first: int, second: int) -> bool: ...
    def target(self, anchor: int, current: Observation) -> Native | None: ...
    def write(self, target: Native) -> None: ...


@dataclass(frozen=True)
class Plugin:
    id: str
    label: str
    files: tuple[str, ...]
    description: str = ''
    actions: tuple[dict, ...] = ()
    setup: Callable | None = None
    reader: Callable | None = None  # (root, book) -> context manager yielding Reader or None
    books: Callable | None = None  # (root) -> iterable of verified Book objects
    discover: Callable | None = None  # (root, connected plugin IDs) -> status counts and optional matches list
    disconnect: Callable | None = None
    catalog_requires: tuple[str, ...] = ()
    reader_session: Callable | None = None  # (root) -> context manager yielding a job-scoped reader factory

    def catalog_ready(self, connected):
        return self.books is not None and all(key in connected for key in self.catalog_requires)

    def connected(self, root):
        return all((root / name).is_file() for name in self.files)
