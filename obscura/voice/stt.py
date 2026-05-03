"""obscura.voice.stt — Speech-to-text client.

Streams raw PCM audio to an STT service and returns the transcript.
Supports two backends:

1. **Anthropic WebSocket STT** (default) — streams to Claude's STT endpoint
2. **Local whisper** (fallback) — uses OpenAI Whisper via subprocess

The client handles keepalives, partial transcripts, and finalization.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
import wave
from pathlib import Path

logger = logging.getLogger(__name__)

# STT configuration.
DEFAULT_LANGUAGE = "en"
KEEPALIVE_INTERVAL = 8.0  # seconds
FINALIZE_TIMEOUT = 5.0  # seconds after CloseStream


class STTClient:
    """Speech-to-text transcription client.

    Supports Anthropic WebSocket STT and local Whisper fallback.
    """

    def __init__(
        self,
        *,
        language: str = DEFAULT_LANGUAGE,
        api_key: str = "",
    ) -> None:
        self._language = language
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")

    async def transcribe(self, audio_pcm: bytes) -> str:
        """Transcribe raw PCM audio bytes to text.

        Tries WebSocket STT first, falls back to local Whisper.
        """
        if not audio_pcm or len(audio_pcm) < 3200:  # <0.1s of audio
            return ""

        # Try local whisper first (no network dependency).
        whisper_path = shutil.which("whisper")
        if whisper_path is not None:
            return await self._transcribe_whisper(audio_pcm, whisper_path)

        # Try Anthropic WebSocket STT.
        if self._api_key:
            try:
                return await self._transcribe_websocket(audio_pcm)
            except Exception:
                logger.warning(
                    "WebSocket STT failed, trying whisper fallback",
                    exc_info=True,
                )

        # Try whisper via Python module.
        try:
            return await self._transcribe_whisper_python(audio_pcm)
        except Exception:
            logger.warning("Whisper Python fallback failed", exc_info=True)

        return "[transcription unavailable — install whisper or set ANTHROPIC_API_KEY]"

    async def _transcribe_websocket(self, audio_pcm: bytes) -> str:
        """Stream audio to Anthropic's WebSocket STT endpoint."""
        try:
            import websockets
        except ImportError:
            msg = "WebSocket STT requires: uv pip install websockets"
            raise RuntimeError(msg)

        url = (
            "wss://api.anthropic.com/api/ws/speech_to_text/voice_stream"
            f"?encoding=linear16&sample_rate=16000&channels=1"
            f"&language={self._language}"
            f"&endpointing_ms=300&utterance_end_ms=1000"
        )

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "x-api-key": self._api_key,
        }

        transcript_parts: list[str] = []

        async with websockets.connect(url, additional_headers=headers) as ws:
            # Send audio in chunks.
            chunk_size = 4096
            for i in range(0, len(audio_pcm), chunk_size):
                await ws.send(audio_pcm[i : i + chunk_size])
                await asyncio.sleep(0.01)  # pace to avoid overwhelming

            # Signal end of audio.
            await ws.send(json.dumps({"type": "CloseStream"}))

            # Collect transcript with timeout.
            try:
                async with asyncio.timeout(FINALIZE_TIMEOUT):
                    async for message in ws:
                        if isinstance(message, str):
                            data = json.loads(message)
                            msg_type = data.get("type", "")
                            if msg_type == "TranscriptText":
                                text = data.get("text", "")
                                is_final = data.get("is_final", False)
                                if is_final and text:
                                    transcript_parts.append(text)
                            elif msg_type == "TranscriptError":
                                logger.warning("STT error: %s", data.get("error", ""))
                                break
                            elif msg_type == "TranscriptEndpoint":
                                break
            except TimeoutError:
                logger.debug(
                    "suppressed exception in _transcribe_websocket", exc_info=True
                )

        return " ".join(transcript_parts).strip()

    async def _transcribe_whisper(self, audio_pcm: bytes, whisper_path: str) -> str:
        """Transcribe using local Whisper CLI."""
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = tmp.name
            # Write WAV header + PCM data.
            with wave.open(tmp_path, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)  # 16-bit
                wf.setframerate(16000)
                wf.writeframes(audio_pcm)

        try:
            proc = await asyncio.create_subprocess_exec(
                whisper_path,
                tmp_path,
                "--language",
                self._language,
                "--model",
                "base",
                "--output_format",
                "txt",
                "--output_dir",
                str(Path(tmp_path).parent),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.communicate(), timeout=30.0)

            # Whisper outputs to <input>.txt
            txt_path = Path(tmp_path).with_suffix(".txt")
            if txt_path.exists():
                return txt_path.read_text(encoding="utf-8").strip()
            return ""
        finally:
            Path(tmp_path).unlink(missing_ok=True)
            Path(tmp_path).with_suffix(".txt").unlink(missing_ok=True)

    async def _transcribe_whisper_python(self, audio_pcm: bytes) -> str:
        """Transcribe using whisper Python package."""
        import importlib

        whisper_mod = importlib.import_module("whisper")

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = tmp.name
            with wave.open(tmp_path, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(16000)
                wf.writeframes(audio_pcm)

        try:
            # Run in thread pool to avoid blocking.
            loop = asyncio.get_event_loop()
            model = await loop.run_in_executor(None, whisper_mod.load_model, "base")
            result = await loop.run_in_executor(
                None,
                lambda: model.transcribe(tmp_path, language=self._language),
            )
            return result.get("text", "").strip()
        finally:
            Path(tmp_path).unlink(missing_ok=True)
