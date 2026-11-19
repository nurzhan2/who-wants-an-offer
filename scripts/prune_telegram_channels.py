"""Remove dead and missing channels from telegram_channels.yaml, naming each.

The connector records, per channel, whether its newest post is within 30 days
(``status: live | dead``) and whether its username resolved at all
(``status: missing``), in ``source_state`` under ``channel:<username>``. This
script reads those records and rewrites the yaml without the ones that failed.

    uv run python scripts/prune_telegram_channels.py          # show only
    uv run python scripts/prune_telegram_channels.py --apply  # rewrite the file

A channel that was never checked is kept: "not looked at yet" is not "dead".
Every removed channel is printed with its reason — nothing disappears silently.
"""

import argparse
import asyncio

import yaml  # type: ignore[import-untyped]
from sqlalchemy import select

from app.db.models import SourceState
from app.db.session import session_factory
from app.sources.telegram import CHANNELS_FILE, STATE_CHANNEL


async def statuses() -> dict[str, str]:
    """username -> live | dead | missing, as the last run recorded it."""
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(SourceState.key, SourceState.value).where(
                    SourceState.source_slug == "telegram",
                    SourceState.key.startswith(STATE_CHANNEL),
                )
            )
        ).all()
    return {key.removeprefix(STATE_CHANNEL): str(value.get("status", "")) for key, value in rows}


def prune(document: dict[str, object], known: dict[str, str]) -> tuple[list[object], list[str]]:
    """The channels to keep, and one line per channel removed."""
    kept: list[object] = []
    removed: list[str] = []
    for entry in document.get("channels", []) or []:  # type: ignore[attr-defined]
        name = str(entry.get("username", "")) if isinstance(entry, dict) else ""
        status = known.get(name)
        if status in {"dead", "missing"}:
            reason = "нет постов за 30 дней" if status == "dead" else "username не найден"
            removed.append(f"@{name}: {reason}")
        else:
            kept.append(entry)
    return kept, removed


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--apply", action="store_true", help="переписать telegram_channels.yaml")
    args = parser.parse_args()
    document = yaml.safe_load(CHANNELS_FILE.read_text(encoding="utf-8")) or {}
    kept, removed = prune(document, asyncio.run(statuses()))
    for line in removed:
        print(f"удаляю {line}" if args.apply else f"удалил бы {line}")
    if not removed:
        print("Удалять нечего: мёртвых и ненайденных каналов в записях нет.")
    if args.apply and removed:
        header = CHANNELS_FILE.read_text(encoding="utf-8").split("channels:", 1)[0]
        body = yaml.safe_dump({"channels": kept}, allow_unicode=True, sort_keys=False)
        CHANNELS_FILE.write_text(header + body, encoding="utf-8")
        print(f"Записано: {CHANNELS_FILE} — осталось {len(kept)}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
