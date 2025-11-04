from __future__ import annotations

from collections import defaultdict
from shutil import get_terminal_size
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from collections.abc import Callable, Iterable


def group_by[T, U](f: Callable[[T], U], it: Iterable[T]) -> dict[U, list[T]]:
    d = defaultdict(list)
    for el in it:
        d[f(el)].append(el)
    return d


def clear() -> None:
    print("\033[H\033[J", end="")


def print_line() -> None:
    print("=" * get_terminal_size().columns)
