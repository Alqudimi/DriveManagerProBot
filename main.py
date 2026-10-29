from __future__ import annotations

import asyncio
import logging

from drive_telegram_manager.config import get_settings
from drive_telegram_manager.telegram.bot import run


def main() -> None:
    logging.basicConfig(
        level=getattr(logging, get_settings().log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(run(get_settings()))


if __name__ == "__main__":
    main()
