"""Unit tests for the inline-keyboard builders in bot.py."""
from aiogram.types import InlineKeyboardMarkup

from bot import get_back_button, get_main_keyboard


def _all_buttons(markup):
    return [btn for row in markup.inline_keyboard for btn in row]


class TestMainKeyboard:
    def test_returns_markup(self):
        assert isinstance(get_main_keyboard(), InlineKeyboardMarkup)

    def test_expected_callbacks_present(self):
        callbacks = {b.callback_data for b in _all_buttons(get_main_keyboard())}
        assert {
            "live_matches", "today", "sport_football", "sport_hockey",
            "sport_esports", "my_bank", "settings",
        } <= callbacks

    def test_every_button_has_text_and_callback(self):
        for b in _all_buttons(get_main_keyboard()):
            assert b.text
            assert b.callback_data


class TestBackButton:
    def test_returns_markup_with_single_back_action(self):
        markup = get_back_button()
        buttons = _all_buttons(markup)
        assert len(buttons) == 1
        assert buttons[0].callback_data == "back_to_start"
