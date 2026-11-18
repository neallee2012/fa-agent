"""Saved memory-settings rows keep their original per-scope keys so old rows stay readable."""

from shared_context.memory_bridge_settings import settings_head_key, settings_key


def test_legacy_a_settings_keys_are_unchanged():
    assert settings_key("authority", "same-memory-setting") == (
        "ca0904956c2bab62c2837b0bdcccaf4783eccf71b725293c0a9281e1bfb0c003")
    assert settings_head_key("authority") == "18077958c2d5641fb9c44a6fbf1816d1695ca3290c292bd564352daac4c7828b"
