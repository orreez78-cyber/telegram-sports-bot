"""Shared fixtures for tests that exercise the async SQLite data layer."""
import pytest_asyncio

import bot


@pytest_asyncio.fixture
async def db(tmp_path):
    """Provide a fresh, isolated SQLite database for each test.

    bot.py keeps a module-global connection (`_db_conn`) and database path
    (`DB_NAME`); we point them at a temporary file and reset the connection so
    tests never touch the real `sports_bot.db`.
    """
    original_name = bot.DB_NAME
    original_conn = bot._db_conn

    bot.DB_NAME = str(tmp_path / "test.db")
    bot._db_conn = None

    conn = await bot.get_db()
    await bot.init_db()
    try:
        yield conn
    finally:
        try:
            await conn.close()
        except Exception:
            pass
        bot._db_conn = original_conn
        bot.DB_NAME = original_name
