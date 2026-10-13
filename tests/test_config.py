import pytest
from pydantic import ValidationError

from app.config import Settings


def test_settings_have_no_direct_thingspeak_configuration(monkeypatch):
    monkeypatch.setenv("THINGSPEAK_CHANNEL_ID", "3499301")
    monkeypatch.setenv("THINGSPEAK_READ_API_KEY", "should-not-be-read")
    settings = Settings(_env_file=None)

    assert not hasattr(settings, "thingspeak_channel_id")
    assert not hasattr(settings, "thingspeak_read_api_key")
    assert "should-not-be-read" not in settings.model_dump_json()


def test_java_api_url_is_the_only_upstream_and_must_be_http():
    assert Settings(java_api_url="http://localhost:8080/").java_api_url == "http://localhost:8080"
    with pytest.raises(ValidationError):
        Settings(java_api_url="ftp://java.example.test")


def test_internal_key_must_be_long_enough_to_enable_service_to_service_reads():
    assert Settings(internal_api_key="short").internal_key_configured is False
    assert Settings(internal_api_key="x" * 32).internal_key_configured is True


def test_local_defaults_use_loopback_java_api_and_explicit_local_origins():
    settings = Settings(_env_file=None)

    assert settings.java_api_url == "http://localhost:8080"
    assert "http://localhost:5173" in settings.configured_origins
    assert "*" not in settings.configured_origins
