from __future__ import annotations

import os

import pytest

from drive_telegram_manager.config import Settings
from drive_telegram_manager.telegram.bot import build_bot


@pytest.mark.skipif(
    os.getenv("RUN_LIVE_TELEGRAM_TESTS") != "1", reason="requires a running local Bot API server and real credentials"
)
@pytest.mark.asyncio
async def test_get_me_through_local_api():
    settings = Settings()
    bot = build_bot(settings)
    try:
        result = await bot.get_me()
        assert result.id > 0
        assert bot.session.api.base.startswith("http://127.0.0.1:")
    finally:
        await bot.session.close()
