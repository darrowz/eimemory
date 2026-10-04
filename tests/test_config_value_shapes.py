"""Ordinary config shape/type tests using only in-memory file stand-ins."""
import json
from types import SimpleNamespace

import pytest

from eimemory.config.loader import _load_file_payload, _integer_setting, _string_setting


@pytest.mark.parametrize("value", [[], [["rpc_port", 1234]], None, "settings", 123])
def test_config_requires_a_json_object(value):
    path = SimpleNamespace(exists=lambda: True, read_text=lambda **kwargs: json.dumps(value))
    with pytest.raises(ValueError, match="JSON object"):
        _load_file_payload(path)


def test_object_config_is_preserved():
    path = SimpleNamespace(exists=lambda: True, read_text=lambda **kwargs: '{"rpc_port": 1234}')
    assert _load_file_payload(path) == {"rpc_port": 1234}


@pytest.mark.parametrize("value", [None, True, False, 1.5, [], {}])
def test_integer_settings_reject_silent_coercion(value):
    with pytest.raises(ValueError, match="rpc_port must be an integer"):
        _integer_setting(value, "rpc_port")


@pytest.mark.parametrize("value,expected", [(8091, 8091), ("8091", 8091), (" 8091 ", 8091), (0, 0)])
def test_valid_integer_settings_remain_compatible(value, expected):
    assert _integer_setting(value, "rpc_port") == expected


@pytest.mark.parametrize("value", [None, 123, [], {}])
def test_string_settings_reject_nonstring_values(value):
    with pytest.raises(ValueError, match="rpc_host must be a string"):
        _string_setting({"rpc_host": value}, "rpc_host", "127.0.0.1")


def test_missing_string_setting_keeps_default():
    assert _string_setting({}, "rpc_host", "127.0.0.1") == "127.0.0.1"


@pytest.mark.parametrize("value", [[], {}])
def test_optional_port_reports_field_error_instead_of_unhashable_type(monkeypatch, value):
    from eimemory.config import loader
    monkeypatch.setenv("EIMEMORY_CONFIG_PATH", "/synthetic/settings.json")
    monkeypatch.setattr(loader, "_load_file_payload", lambda *args, **kwargs: {"rpc_loopback_health_port": value})
    with pytest.raises(ValueError, match="rpc_loopback_health_port must be an integer"):
        loader.load_settings()
