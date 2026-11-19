"""The Windows Task Scheduler entry that runs ``wwao daily`` every morning.

    python -m wwao install-schedule [--at 08:30]
    python -m wwao uninstall-schedule

``schtasks`` rather than a service or a resident process: the owner's machine is
a laptop, the job is one run a day, and the Task Scheduler is what Windows
already has for that — it survives a reboot, shows the last result, and can be
removed by hand from its own window.

**What the task runs cannot send anything.** It runs ``python -m wwao daily``,
which runs the chain and the summary (``scripts/daily.py``): it builds the queue
and stops there. Applications leave only after the owner confirms a batch on
the dashboard. There is no flag here that changes that, and ``apply`` refuses to
start without a terminal anyway.

Local time, because that is what ``schtasks /ST`` means and what the owner means
by «08:30». Like the rest of this package it imports neither ``app`` nor
``agent``; the one command it builds is data.
"""

import re
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Final, TextIO

#: The task's name in the Task Scheduler, under the root folder.
TASK_NAME: Final[str] = "WhoWantsAnOffer Daily"
DEFAULT_AT: Final[str] = "08:30"

#: Runs one command, returns its exit code. Injected so tests see the command line.
Runner = Callable[[Sequence[str]], int]


def _valid_time(at: str) -> str:
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", at):
        raise ValueError(f"время в формате ЧЧ:ММ, получено {at!r}")
    return at


def daily_command(repo: Path, python: str | None = None) -> str:
    """The command line the task runs: this interpreter, ``-m wwao daily``, in the repo."""
    interpreter = python or sys.executable
    # schtasks has no working-directory option, so the command changes into the
    # repository itself. cmd /c keeps the quoting to one well-understood layer.
    return f'cmd /c cd /d "{repo}" && "{interpreter}" -m wwao daily'


def install(
    *,
    repo: Path,
    at: str = DEFAULT_AT,
    run: Runner,
    out: TextIO,
    err: TextIO,
    platform: str = sys.platform,
    python: str | None = None,
) -> int:
    """Create (or replace) the daily task."""
    if not platform.startswith("win"):
        print(
            "Планировщик заданий есть только в Windows. На Linux/macOS добавьте в crontab:\n"
            f"  {at[3:]} {at[:2]} * * * cd {repo} && {python or sys.executable} -m wwao daily",
            file=err,
        )
        return 3
    try:
        when = _valid_time(at)
    except ValueError as error:
        print(str(error), file=err)
        return 2
    command = [
        "schtasks",
        "/Create",
        "/F",
        "/SC",
        "DAILY",
        "/ST",
        when,
        "/TN",
        TASK_NAME,
        "/TR",
        daily_command(repo, python),
    ]
    code = run(command)
    if code == 0:
        print(
            f"Задача «{TASK_NAME}» создана: каждый день в {when} по местному времени — "
            "сбор, подбор, письма и сводка в Telegram. Отклики она не отправляет.",
            file=out,
        )
    else:
        print(f"schtasks не создал задачу (код {code}).", file=err)
    return code


def uninstall(*, run: Runner, out: TextIO, err: TextIO, platform: str = sys.platform) -> int:
    """Remove the daily task."""
    if not platform.startswith("win"):
        print("Планировщик заданий есть только в Windows; уберите строку из crontab.", file=err)
        return 3
    code = run(["schtasks", "/Delete", "/F", "/TN", TASK_NAME])
    print(
        f"Задача «{TASK_NAME}» удалена."
        if code == 0
        else f"schtasks не удалил задачу (код {code}).",
        file=out if code == 0 else err,
    )
    return code
