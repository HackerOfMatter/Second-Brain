"""Renaming a file, on an operating system where renaming a file can fail.

On POSIX, `os.replace` cannot fail for a reason that would go away if you
tried again a moment later. On Windows it can, routinely, and for reasons that
have nothing to do with this program:

  * **Another handle is open on the destination.** Windows' `MoveFileEx`
    refuses to replace a file someone else has open — and "someone else"
    includes a reader. Two of this system's own writers landing on one note
    milliseconds apart is enough, and `Vault.write`'s docstring says that is
    the normal case, not the edge one.
  * **The antivirus scanner is reading the file we just wrote.** Defender
    opens every newly created file. That handle lives for a few milliseconds
    and it makes the very next `replace` fail with Access Denied, in
    single-threaded code, non-deterministically.
  * **A sync client or an editor is watching the folder.** Obsidian and
    OneDrive both stat and open files as they appear.

None of those is a real failure. They are a *busy* signal wearing an error's
clothes, and the correct response to all three is to wait a few milliseconds
and do it again. This is what git, pip and CPython's own installer do on
Windows, for exactly these error codes.

Retrying is scoped as narrowly as it can be. Only these Windows error numbers
are treated as transient; anything else — a bad path, a read-only file, a full
disk — raises immediately and unchanged, because retrying a real error just
turns a clear failure into a slow one. On POSIX `winerror` is always None, so
nothing here retries and the behaviour is byte-for-byte what it was before.

Why this exists as its own module rather than a helper inside `vault.py`:
`cards.py` rewrites a deck on **every single answer**, which is the highest
write rate in the system, and `intake.py` moves a file the reader has only
just closed. Both need the same treatment, and a second copy of it would
inevitably drift from the first.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Union

PathLike = Union[str, Path]

#: Windows error numbers that mean "something else is holding this right now".
#:   5   ERROR_ACCESS_DENIED
#:   32  ERROR_SHARING_VIOLATION      — the classic "used by another process"
#:   33  ERROR_LOCK_VIOLATION
#:   145 ERROR_DIR_NOT_EMPTY          — a directory whose contents are closing
#:   183 ERROR_ALREADY_EXISTS         — a rename that lost a race
TRANSIENT_WINERRORS = frozenset({5, 32, 33, 145, 183})

#: 8 attempts at 8ms doubling to a 250ms cap is about 1.2 s of patience in the
#: worst case, and in the ordinary case the second attempt succeeds. Long
#: enough to outlast a virus scan of a small text file; short enough that a
#: genuinely stuck file still fails inside one HTTP request.
ATTEMPTS = 8
FIRST_DELAY = 0.008
MAX_DELAY = 0.25


def is_transient(exc: BaseException) -> bool:
    """A busy signal, not a failure. False for everything on POSIX."""
    return isinstance(exc, OSError) and getattr(exc, "winerror", None) in TRANSIENT_WINERRORS


def _raw_replace(src: PathLike, dst: PathLike) -> None:
    os.replace(os.fspath(src), os.fspath(dst))


def _raw_move(src: PathLike, dst: PathLike) -> None:
    shutil.move(os.fspath(src), os.fspath(dst))


def _retrying(operation, src: PathLike, dst: PathLike) -> None:
    delay = FIRST_DELAY
    for attempt in range(ATTEMPTS):
        try:
            operation(src, dst)
            return
        except OSError as exc:
            if attempt == ATTEMPTS - 1 or not is_transient(exc):
                raise
            time.sleep(delay)
            delay = min(delay * 2, MAX_DELAY)


def replace(src: PathLike, dst: PathLike) -> None:
    """`os.replace`, retried while Windows says the destination is busy.

    Still atomic: every attempt is one `MoveFileEx`, so a reader sees either
    the old file or the new one and never a partial write. Retrying does not
    weaken that — it only decides how long we are willing to wait for the
    single atomic step to be allowed to happen.
    """
    _retrying(_raw_replace, src, dst)


def move(src: PathLike, dst: PathLike) -> None:
    """`shutil.move`, with the same patience.

    Used where a file changes name or folder rather than content: a note whose
    title was edited, a bucket transition, and the Drop folder filing an
    original away moments after reading it.
    """
    _retrying(_raw_move, src, dst)
