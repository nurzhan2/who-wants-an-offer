"""``install-schedule`` / ``uninstall-schedule``: one Task Scheduler entry, nothing else."""

import io
from collections.abc import Sequence
from pathlib import Path

from wwao import cli, schedule


class Recorder:
    def __init__(self, code: int = 0) -> None:
        self.code = code
        self.commands: list[list[str]] = []

    def __call__(self, command: Sequence[str]) -> int:
        self.commands.append(list(command))
        return self.code


def test_install_creates_a_daily_task_at_half_past_eight_running_wwao_daily() -> None:
    run, out, err = Recorder(), io.StringIO(), io.StringIO()

    code = schedule.install(
        repo=Path("C:/repo"), run=run, out=out, err=err, platform="win32", python="C:/py.exe"
    )

    assert code == 0
    [command] = run.commands
    assert command[:7] == ["schtasks", "/Create", "/F", "/SC", "DAILY", "/ST", "08:30"]
    assert command[command.index("/TN") + 1] == schedule.TASK_NAME
    action = command[command.index("/TR") + 1]
    assert action.endswith('"C:/py.exe" -m wwao daily')
    assert "apply" not in action and "--send" not in action, "the task cannot send"
    assert "не отправляет" in out.getvalue()


def test_install_refuses_a_time_that_is_not_hh_mm() -> None:
    run, err = Recorder(), io.StringIO()
    code = schedule.install(
        repo=Path("."), at="8:30am", run=run, out=io.StringIO(), err=err, platform="win32"
    )
    assert code == 2 and run.commands == []


def test_outside_windows_it_says_what_to_put_in_crontab_instead() -> None:
    run, err = Recorder(), io.StringIO()
    code = schedule.install(
        repo=Path("/repo"), run=run, out=io.StringIO(), err=err, platform="linux", python="py"
    )
    assert code == 3 and run.commands == []
    assert "30 08 * * * cd /repo && py -m wwao daily" in err.getvalue()


def test_uninstall_deletes_the_same_task() -> None:
    run, out = Recorder(), io.StringIO()
    assert schedule.uninstall(run=run, out=out, err=io.StringIO(), platform="win32") == 0
    assert run.commands == [["schtasks", "/Delete", "/F", "/TN", schedule.TASK_NAME]]


def test_a_failing_schtasks_is_reported() -> None:
    run, err = Recorder(code=1), io.StringIO()
    assert schedule.uninstall(run=run, out=io.StringIO(), err=err, platform="win32") == 1
    assert "не удалил" in err.getvalue()


def test_the_cli_routes_daily_to_its_script_and_the_schedule_commands() -> None:
    run = Recorder()
    assert cli.main(["daily", "--no-telegram"], run=run, stdout=io.StringIO()) == 0
    assert run.commands[0][-2:] == [str(cli.SCRIPTS / "daily.py"), "--no-telegram"]

    run = Recorder()
    cli.main(["uninstall-schedule"], run=run, stdout=io.StringIO(), stderr=io.StringIO())
    if run.commands:  # only on Windows does it reach schtasks
        assert run.commands[0][0] == "schtasks"
