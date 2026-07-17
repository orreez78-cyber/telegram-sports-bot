"""Guards against the missing-import regression in send_predictions_job.

`send_predictions_job` references TelegramRetryAfter / TelegramForbiddenError /
TelegramBadRequest; if they are not imported into bot.py the handler raises
NameError the first time Telegram returns an error.
"""
import bot


def test_telegram_exceptions_are_imported():
    assert bot.TelegramRetryAfter is not None
    assert bot.TelegramForbiddenError is not None
    assert bot.TelegramBadRequest is not None


def test_sport_columns_whitelist():
    assert set(bot.SPORT_COLUMNS) == {"football", "hockey", "esports"}
