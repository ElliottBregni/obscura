"""obscura.voice — Push-to-talk voice input with speech-to-text.

Provides audio capture (via SoX ``rec`` or ALSA ``arecord``) and
streaming speech-to-text transcription.

Architecture:
  1. AudioCapture: manages subprocess recording (SoX or arecord)
  2. STTClient: streams audio to a WebSocket STT endpoint
  3. VoiceSession: ties capture + STT together for push-to-talk UX

Usage::

    session = VoiceSession()
    await session.start_recording()
    # ... user holds key ...
    transcript = await session.stop_and_transcribe()
    print(transcript)
"""

from obscura.voice.capture import AudioCapture, check_voice_dependencies
from obscura.voice.session import VoiceSession
from obscura.voice.stt import STTClient

__all__ = ["AudioCapture", "STTClient", "VoiceSession", "check_voice_dependencies"]
