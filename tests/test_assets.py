import pytest

from iassistpro import assets
from iassistpro.assets import ModelStatus, health, prepare, required_assets
from iassistpro.config import Settings


def test_default_assets_are_whisper_and_piper(settings):
    assert [(a.id, a.size_mb) for a in required_assets(settings)] == [("whisper:base.en", 145), ("piper:en_US-lessac-medium", 63)]


def test_kokoro_asset_only_when_selected(monkeypatch):
    monkeypatch.setenv("TTS_PROVIDER", "kokoro")
    assert [a.id for a in required_assets(Settings())] == ["whisper:base.en", "kokoro:int8"]


async def test_progress_then_ready(settings, monkeypatch):
    snaps = []
    monkeypatch.setattr(assets, "_fetch_whisper", lambda s: snaps.append(status.snapshot()))
    monkeypatch.setattr(assets, "_fetch_piper", lambda s: snaps.append(status.snapshot()))
    status = ModelStatus()
    await prepare(settings, status, warm=lambda: snaps.append(status.snapshot()))
    snaps.append(status.snapshot())
    assert [(x["state"], x["done_mb"], x["total_mb"]) for x in snaps] == [
        ("loading_models", 0, 208), ("loading_models", 145, 208), ("loading_models", 208, 208), ("ready", 208, 208)]
    assert snaps[0]["current"] == "whisper:base.en"


async def test_download_failure_is_reported(settings, monkeypatch):
    monkeypatch.setattr(assets, "_fetch_whisper", lambda s: None)

    def boom(s):
        raise OSError("network down")

    monkeypatch.setattr(assets, "_fetch_piper", boom)
    status = ModelStatus()
    warmed = []
    await prepare(settings, status, warm=lambda: warmed.append(1))
    assert status.state == "error" and status.error == "model_download_failed:piper:en_US-lessac-medium" and not warmed


def test_missing_key_is_an_error_state(settings):
    status = ModelStatus()
    status.state = "ready"
    h = health(settings, status, None)
    assert (h["status"], h["error"]) == ("error", "missing_key:ANTHROPIC_API_KEY")


def test_fake_llm_needs_no_key(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    status = ModelStatus()
    status.state = "ready"
    h = health(Settings(), status, "abc")
    assert (h["status"], h["error"], h["live_session"], h["progress"]) == ("ready", None, "abc", None)
    assert h["version"] == "0.1.0" and h["contract_version"] == 1 and h["providers"]["llm"]["name"] == "fake"


def test_loading_health_has_progress(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    status = ModelStatus()
    status.total_mb, status.done_mb, status.current = 208, 145, "piper:en_US-lessac-medium"
    h = health(Settings(), status, None)
    assert h["status"] == "loading_models" and h["progress"] == {"done_mb": 145, "total_mb": 208, "current": "piper:en_US-lessac-medium"}


def test_unknown_provider_is_a_config_error(monkeypatch):
    monkeypatch.setenv("TTS_PROVIDER", "nope")
    h = health(Settings(), ModelStatus(), None)
    assert h["status"] == "error" and h["error"].startswith("config:unknown TTS_PROVIDER 'nope'")
