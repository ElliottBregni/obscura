"""obscura.voice.session — Push-to-talk voice session.

Ties ``AudioCapture`` and ``STTClient`` together into a simple
push-to-talk workflow: start recording → stop → get transcript.
"""

from __future__ import annotations

import logging

from obscura.voice.capture import AudioCapture, check_voice_dependencies
from obscura.voice.stt import STTClient

logger = logging.getLogger(__name__)


class VoiceSession:
    """Push-to-talk voice input session.

    Usage::

        session = VoiceSession()
        if not session.is_available:
            print("Install SoX: brew install sox")

    Return:
        await session.start_recording()
        # ... user speaks ...
        transcript = await session.stop_and_transcribe()
        print(f"You said: {transcript}")

    """

    def __init__(
        self,
        *,
        language: str = "en",
        api_key: str = "",
    ) -> None:
        self._capture = AudioCapture()
        self._stt = STTClient(language=language, api_key=api_key)
        self._deps = check_voice_dependencies()

    @property
    def is_available(self) -> bool:
        """True if an audio capture backend is available."""
        return self._deps.available

    @property
    def backend(self) -> str:
        """Name of the audio backend in use."""
        return self._deps.backend

    @property
    def install_hint(self) -> str:
        """Installation instructions if not available."""
        return self._deps.install_hint

    @property
    def is_recording(self) -> bool:
        """True if currently recording."""
        return self._capture.is_recording

    async def start_recording(self) -> None:
        """Start recording from the microphone."""
        if not self._deps.available:
            msg = f"No audio backend. {self._deps.install_hint}"
            raise RuntimeError(msg)
        if self._capture.is_recording:
            logger.warning("Already recording — ignoring start_recording()")
            return
        await self._capture.start()
        logger.info("Voice recording started (backend=%s)", self._deps.backend)

    async def stop_and_transcribe(self) -> str:
        """Stop recording and return the transcript."""
        if not self._capture.is_recording:
            return ""
        audio_data = await self._capture.stop()
        if not audio_data:
            return ""
        duration_s = len(audio_data) / (16000 * 2)  # 16kHz, 16-bit
        logger.info("Voice recording stopped: %.1fs of audio", duration_s)
        transcript = await self._stt.transcribe(audio_data)
        logger.info("Transcript: %s", transcript[:100])
        return transcript

    async def cancel(self) -> None:
        """Cancel recording without transcribing."""
        if self._capture.is_recording:
            await self._capture.stop()
            logger.info("Voice recording cancelled")
