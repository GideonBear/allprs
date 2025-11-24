from __future__ import annotations

from collections import defaultdict
from shutil import get_terminal_size
from typing import TYPE_CHECKING

from prompt_toolkit import PromptSession
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
from prompt_toolkit.patch_stdout import patch_stdout


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


kb = KeyBindings()


@kb.add("<any>")
def _(event: KeyPressEvent) -> None:
    event.app.exit(result=event.key_sequence[0].key)


async def areadchar(prompt: str = "") -> str:
    session: PromptSession[str] = PromptSession()
    with patch_stdout():
        return await session.prompt_async(prompt, key_bindings=kb)
