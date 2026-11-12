# SPDX-License-Identifier: AGPL-3.0-or-later
"""Local speech recognition in a separate process; never imports desktop input.

The model is downloaded only by explicit preparation. Transcription accepts
bounded mono PCM WAV bytes and loads local files only. No recording is saved.
"""
from __future__ import annotations

import argparse
import base64
import importlib.util
import io
import json
import os
import sys
import wave
from pathlib import Path

MAX_SECONDS = 30
SAMPLE_RATE = 16000
MAX_AUDIO_BYTES = SAMPLE_RATE * MAX_SECONDS * 2 + 4096
MODEL = "tiny.en"


class VoiceError(ValueError):
    pass


def default_model_dir() -> Path:
    if os.environ.get('CEC_VOICE_MODEL_DIR'):
        return Path(os.environ['CEC_VOICE_MODEL_DIR'])
    if sys.platform == 'win32':
        base = Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData' / 'Local'))
    else:
        base = Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache'))
    return base / 'embodied-cursor' / 'voice' / MODEL


def decode_audio(encoded: str) -> bytes:
    if not isinstance(encoded, str) or len(encoded) > (MAX_AUDIO_BYTES + 2) // 3 * 4:
        raise VoiceError('Record at most 30 seconds of speech')
    try:
        raw = base64.b64decode(encoded, validate=True)
        with wave.open(io.BytesIO(raw), 'rb') as audio:
            frames = audio.getnframes()
            if (audio.getnchannels() != 1 or audio.getsampwidth() != 2
                    or audio.getframerate() != SAMPLE_RATE or audio.getcomptype() != 'NONE'
                    or not SAMPLE_RATE // 4 <= frames <= SAMPLE_RATE * MAX_SECONDS):
                raise VoiceError('Voice input must be 0.25–30 seconds of mono 16 kHz PCM WAV')
            pcm = audio.readframes(frames)
            if len(pcm) != frames * 2 or len(raw) > MAX_AUDIO_BYTES:
                raise VoiceError('The voice recording is incomplete or too large')
            return pcm
    except (wave.Error, EOFError, ValueError) as exc:
        if isinstance(exc, VoiceError):
            raise
        raise VoiceError('The voice recording is not a valid PCM WAV file') from None


class VoiceEngine:
    def __init__(self, model_dir: Path | None = None):
        self.model_dir = model_dir or default_model_dir()
        self._model = None

    def status(self) -> dict:
        installed = importlib.util.find_spec('faster_whisper') is not None
        prepared = all((self.model_dir / name).is_file() for name in ('.cec-ready', 'model.bin', 'config.json'))
        return {'available': installed, 'prepared': installed and prepared,
                'model': MODEL, 'local': True, 'maxSeconds': MAX_SECONDS,
                'setup': 'Install the voice extra in the selected Python runtime: python -m pip install -e ".[voice]"'}

    def _load(self):
        if not self.status()['available']:
            raise VoiceError(self.status()['setup'])
        try:
            from faster_whisper import WhisperModel
            # An utterance can never trigger a network download.
            self._model = WhisperModel(str(self.model_dir), device='cpu', compute_type='int8',
                                       cpu_threads=min(os.cpu_count() or 2, 4), local_files_only=True)
        except Exception:
            raise VoiceError('The local speech model could not load. Check the voice runtime and prepare the model again') from None

    def prepare(self) -> dict:
        if not self.status()['available']:
            raise VoiceError(self.status()['setup'])
        if self.status()['prepared']:
            self._load()
            return self.status()
        try:
            from faster_whisper.utils import download_model
            self.model_dir.mkdir(parents=True, exist_ok=True)
            download_model(MODEL, output_dir=str(self.model_dir))
        except Exception:
            raise VoiceError('Speech model download failed. Check access to Hugging Face, then retry Prepare local voice') from None
        self._load()
        marker = self.model_dir / '.cec-ready'
        marker.write_text(MODEL, encoding='utf-8')
        return self.status()

    def transcribe(self, encoded: str) -> dict:
        pcm = decode_audio(encoded)
        if self._model is None:
            if not self.status()['prepared']:
                raise VoiceError('Prepare local voice in Settings before recording a command')
            self._load()
        import numpy as np
        samples = np.frombuffer(pcm, dtype='<i2').astype(np.float32) / 32768.0
        if float(np.sqrt(np.mean(samples * samples))) < 0.002:
            raise VoiceError('No speech heard. Move closer to the microphone and try again')
        try:
            segments, info = self._model.transcribe(samples, language='en', beam_size=1,
                vad_filter=True, condition_on_previous_text=False, temperature=0.0,
                without_timestamps=True)
            text = ' '.join(segment.text.strip() for segment in segments
                if segment.no_speech_prob < 0.6 and segment.avg_logprob > -1.0
                and segment.compression_ratio < 2.4).strip()
        except Exception:
            raise VoiceError('Local transcription failed. Try a shorter recording or prepare the speech model again') from None
        if not text:
            raise VoiceError('No clear speech was recognized. Please try again')
        if len(text) > 4000:
            raise VoiceError('The transcript is too long. Record a shorter command')
        return {'text': text, 'language': info.language, 'duration': round(len(samples) / SAMPLE_RATE, 2)}


def serve(engine: VoiceEngine) -> None:
    print(json.dumps({'event': 'ready', 'data': {'voice': True}}), flush=True)
    for line in sys.stdin:
        ident = None
        try:
            if len(line) > 2 * 1024 * 1024:
                raise VoiceError('Voice request is too large')
            request = json.loads(line)
            if not isinstance(request, dict) or type(request.get('id')) is not int:
                raise VoiceError('Invalid voice request')
            ident = request['id']
            op = request.get('op')
            if op == 'status':
                result = engine.status()
            elif op == 'prepare':
                result = engine.prepare()
            elif op == 'transcribe':
                result = engine.transcribe(request.get('audio'))
            else:
                raise VoiceError('Unsupported voice operation')
            response = {'id': ident, 'ok': True, 'result': result}
        except (VoiceError, ValueError, TypeError) as exc:
            response = {'id': ident, 'ok': False, 'error': str(exc)}
        except Exception as exc:
            response = {'id': ident, 'ok': False, 'error': f'Voice runtime failed ({type(exc).__name__})'}
        print(json.dumps(response, ensure_ascii=True), flush=True)


def main():
    parser = argparse.ArgumentParser(description='Prepare or serve local CEC speech recognition')
    parser.add_argument('--prepare', action='store_true')
    parser.add_argument('--status', action='store_true')
    args = parser.parse_args()
    engine = VoiceEngine()
    if args.prepare or args.status:
        try:
            print(json.dumps(engine.prepare() if args.prepare else engine.status()))
        except VoiceError as exc:
            parser.exit(1, f'{exc}\n')
    else:
        serve(engine)


if __name__ == '__main__':
    main()
