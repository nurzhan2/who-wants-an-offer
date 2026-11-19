"""One command a day: collect, embed, score every profile, write letters, report.

    uv run python scripts/daily.py              # the whole day, then the summary
    uv run python scripts/daily.py --no-crawl   # only the summary of what is there
    uv run python scripts/daily.py --no-telegram

Started by ``python -m wwao daily`` and by the scheduled task
``wwao install-schedule`` creates. Sends no application: the queue it builds
waits for the owner's confirmation on the dashboard. See
``app/services/daily.py``.
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.core.logging import configure_logging
from app.services import daily


async def run(*, crawl: bool, telegram: bool) -> int:
    summary = await daily.run_daily(crawl=crawl)
    text = summary.render()
    print(text)
    if summary.chain_lines:
        print("\nЦепочка:")
        for line in summary.chain_lines:
            print(f"  {line}")
    if telegram:
        sent = await daily.notify(text)
        print(
            "\nСводка отправлена в Telegram."
            if sent
            else "\nВ Telegram не отправлено: нет TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID "
            "или бот не ответил (подробности в журнале)."
        )
    return 1 if any(item.startswith("цепочка:") for item in summary.broken) else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--no-crawl", action="store_true", help="только сводка, без цепочки")
    parser.add_argument("--no-telegram", action="store_true", help="не отправлять сводку")
    args = parser.parse_args()
    configure_logging()
    return asyncio.run(run(crawl=not args.no_crawl, telegram=not args.no_telegram))


if __name__ == "__main__":
    raise SystemExit(main())
