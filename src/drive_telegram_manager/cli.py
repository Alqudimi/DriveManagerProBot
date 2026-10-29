from __future__ import annotations

import asyncio

from drive_telegram_manager.config import get_settings
from drive_telegram_manager.telegram.bot import run


def main() -> None:
    asyncio.run(run(get_settings()))
