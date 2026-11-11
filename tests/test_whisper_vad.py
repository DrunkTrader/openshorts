"""The bundled VAD is on by default and WHISPER_VAD=0 turns it off.

A stub model captures the kwargs; faster_whisper is never imported.
"""
import types

import transcribe_backends as tb


class _Model:
    def __init__(self):
        self.kwargs = None

    def transcribe(self, media, **kwargs):
        self.kwargs = kwargs
        return iter([]), types.SimpleNamespace(language="en", duration=1.0)


def _run(monkeypatch):
    model = _Model()
    monkeypatch.setattr(tb, "_get_whisper_model", lambda: (model, "cpu"))
    tb._transcribe_with_whisper("x.wav")
    return model.kwargs


def test_vad_filter_default_on_and_env_off(monkeypatch):
    monkeypatch.delenv("WHISPER_VAD", raising=False)
    assert _run(monkeypatch)["vad_filter"] is True
    monkeypatch.setenv("WHISPER_VAD", "0")
    assert _run(monkeypatch)["vad_filter"] is False


def test_vad_silence_gap_only_when_set(monkeypatch):
    monkeypatch.delenv("WHISPER_VAD", raising=False)
    monkeypatch.delenv("WHISPER_VAD_MIN_SILENCE_MS", raising=False)
    assert "vad_parameters" not in _run(monkeypatch)
    monkeypatch.setenv("WHISPER_VAD_MIN_SILENCE_MS", "800")
    assert _run(monkeypatch)["vad_parameters"] == {"min_silence_duration_ms": 800}
