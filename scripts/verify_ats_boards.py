"""Ask every board in company_boards.yaml once, and say which ones exist.

    uv run python scripts/verify_ats_boards.py
    uv run python scripts/verify_ats_boards.py --ats greenhouse --ats lever
    uv run python scripts/verify_ats_boards.py --json ats-boards.json

The board list was seeded from memory in a container with no network, so every
entry says ``verified: false``. This is the check that turns a guess into a
fact: one request per board, through each connector's own client — the same
user agent, the same token bucket (about one request a second per vendor), the
same retry policy — and a line per board with ``ok`` and the number of jobs,
``404``, or the error.

It changes nothing and stores nothing; it does not edit the YAML. Set
``verified: true`` by hand on the boards that answered with jobs, and fix or
delete the rest. Run by the owner from a machine with network access, never in
tests or CI.

SmartRecruiters reads only its first page here (one request per board), so its
line carries the vendor's own ``totalFound`` next to the page count. A
SmartRecruiters board at zero is worth a look: an unknown company is believed to
answer 200 with an empty list rather than 404.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.core.logging import configure_logging
from app.sources.ats._common import ATS_KEYS, AtsSource, BoardProbe, load_boards_file
from app.sources.http import close_client, get_client
from app.sources.registry import get_source

RULE = "-" * 78  # ASCII: printed to whatever console the owner has


def parse_args() -> argparse.Namespace:
    """Command line."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--ats",
        action="append",
        choices=ATS_KEYS,
        help="проверить только эту ATS (можно повторять); по умолчанию — все",
    )
    parser.add_argument("--json", help="записать результат в этот файл")
    return parser.parse_args()


def line(probe: BoardProbe) -> str:
    """One board's result, for a person to read."""
    if probe.status == "ok":
        total = f", всего по данным ATS: {probe.stated_total}" if probe.stated_total else ""
        invalid = probe.entries - probe.jobs
        broken = f", не разобрано: {invalid}" if invalid else ""
        verdict = f"ok    вакансий: {probe.jobs}{broken}{total}"
    elif probe.status == "missing":
        verdict = f"{probe.http_status or 404}   доски нет"
    else:
        verdict = f"ошибка  {probe.detail}"
    return f"  {probe.ats:<16} {probe.slug:<24} {probe.company:<24} {verdict}"


async def main() -> int:
    """Run it."""
    args = parse_args()
    configure_logging()
    wanted = tuple(args.ats) if args.ats else ATS_KEYS
    boards = load_boards_file()
    client = get_client()
    probes: list[BoardProbe] = []
    try:
        for ats in wanted:
            source = get_source(ats)
            if not isinstance(source, AtsSource):  # pragma: no cover - a wiring mistake
                raise SystemExit(f"{ats}: не ATS-коннектор")
            source.bind(client.bind(source))
            print(RULE)
            print(f"{source.name}: досок {len(boards.for_ats(ats))}")
            for board in boards.for_ats(ats):
                probe = await source.probe_board(board)
                probes.append(probe)
                print(line(probe), flush=True)
    finally:
        await close_client()

    print(RULE)
    ok = sum(1 for probe in probes if probe.status == "ok")
    empty = sum(1 for probe in probes if probe.status == "ok" and probe.jobs == 0)
    missing = sum(1 for probe in probes if probe.status == "missing")
    failed = sum(1 for probe in probes if probe.status == "error")
    print(f"  ответили: {ok} (из них пустых: {empty}), нет доски: {missing}, ошибок: {failed}")
    print("  verified: true ставится в company_boards.yaml вручную.")
    if args.json:
        Path(args.json).write_text(
            json.dumps([probe.model_dump() for probe in probes], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"  результат записан в {args.json}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
