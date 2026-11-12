# SPDX-License-Identifier: AGPL-3.0-or-later
"""Local voice protocol, bounded audio, and offline-only transcription admission."""
import base64
import io
import json
import struct
import sys
import wave
from types import SimpleNamespace

import pytest

from embodied_cursor.voice import VoiceEngine, VoiceError, decode_audio, serve


def wav(frames=8000, rate=16000, channels=1, width=2, value=4000):
    output = io.BytesIO()
    with wave.open(output, 'wb') as audio:
        audio.setnchannels(channels); audio.setsampwidth(width); audio.setframerate(rate)
        audio.writeframes(struct.pack('<h', value) * (frames * channels) if width == 2 else b'\x01' * frames)
    return base64.b64encode(output.getvalue()).decode()


def test_wav_capture_contract_accepts_pcm_and_rejects_truncated_audio():
    encoded = wav()
    assert len(decode_audio(encoded)) == 16000
    raw = base64.b64decode(encoded)
    with pytest.raises(VoiceError, match='incomplete'):
        decode_audio(base64.b64encode(raw[:-200]).decode())


@pytest.mark.parametrize('encoded', [None, '', 'not base64!', wav(frames=10), wav(frames=480001),
                                      wav(rate=48000), wav(channels=2), wav(width=1)])
def test_invalid_or_unbounded_audio_never_reaches_the_model(encoded):
    with pytest.raises(VoiceError):
        decode_audio(encoded)


def test_transcribing_without_preparation_never_downloads_or_loads_a_model(tmp_path, monkeypatch):
    engine = VoiceEngine(tmp_path / 'model')
    monkeypatch.setattr(engine, 'status', lambda: {'available': True, 'prepared': False})
    monkeypatch.setattr(engine, '_load', lambda: pytest.fail('No implicit model load/download'))
    with pytest.raises(VoiceError, match='Prepare local voice'):
        engine.transcribe(wav())
    assert not engine.model_dir.exists()


def test_transcription_uses_samples_in_memory_and_filters_unclear_segments(tmp_path):
    pytest.importorskip('numpy')
    engine = VoiceEngine(tmp_path / 'model')
    class Model:
        def transcribe(self, samples, **options):
            assert len(samples) == 8000 and options['vad_filter']
            assert options['condition_on_previous_text'] is False
            return [
                SimpleNamespace(text='Click Export.', no_speech_prob=.01, avg_logprob=-.1, compression_ratio=1.0),
                SimpleNamespace(text='background hallucination', no_speech_prob=.9, avg_logprob=-2, compression_ratio=3),
            ], SimpleNamespace(language='en')
    engine._model = Model()
    assert engine.transcribe(wav()) == {'text': 'Click Export.', 'language': 'en', 'duration': .5}
    assert not list(tmp_path.iterdir()), 'Audio is never written to disk'
    with pytest.raises(VoiceError, match='No speech heard'):
        engine.transcribe(wav(value=0))


def test_bad_model_output_is_reported_without_raw_runtime_contents(tmp_path):
    pytest.importorskip('numpy')
    engine = VoiceEngine(tmp_path)
    class Model:
        def transcribe(self, *_args, **_kwargs):
            raise RuntimeError('private audio or provider diagnostics')
    engine._model = Model()
    with pytest.raises(VoiceError, match='Local transcription failed') as error:
        engine.transcribe(wav())
    assert 'private' not in str(error.value)


def test_voice_stdio_status_and_bad_requests_do_not_import_desktop_or_execute_tasks(monkeypatch, capsys, tmp_path):
    engine = VoiceEngine(tmp_path)
    monkeypatch.setattr(engine, 'status', lambda: {'available': False, 'prepared': False})
    monkeypatch.setattr(sys, 'stdin', io.StringIO('garbage\n{"id":1,"op":"status"}\n{"id":2,"op":"run","goal":"click Export"}\n'))
    serve(engine)
    replies = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert replies[0]['event'] == 'ready'
    assert replies[2]['id'] == 1 and replies[2]['result']['available'] is False
    assert replies[3]['id'] == 2 and not replies[3]['ok']
    assert not list(tmp_path.iterdir())
