"""obscura.voice.capture — Audio capture via SoX or ALSA.

Records raw PCM audio (16kHz, 16-bit signed, mono) from the system
microphone using whichever backend is available.

Priority:
  1. SoX ``rec`` (cross-platform, preferred)
  2. ALSA ``arecord`` (Linux fallback)
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import platform
import shutil
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Audio format constants (matching STT requirements).
SAMPLE_RATE = 16000
SAMPLE_WIDTH = 16  # bits
CHANNELS = 1
ENCODING = "signed"  # signed 16-bit little-endian


@dataclass
class VoiceDependency:
    """Result of dependency check."""

    available: bool
    backend: str  # "sox" | "arecord" | "none"
    binary_path: str
    missing: list[str]
    install_hint: str


def check_voice_dependencies() -> VoiceDependency:
    """Check which audio capture backend is available."""
    # 1. Check SoX (rec command)
    rec_path = shutil.which("rec")
    if rec_path:
        return VoiceDependency(
            available=True,
            backend="sox",
            binary_path=rec_path,
            missing=[],
            install_hint="",
        )

    # 2. Check ALSA (arecord)
    arecord_path = shutil.which("arecord")
    if arecord_path:
        return VoiceDependency(
            available=True,
            backend="arecord",
            binary_path=arecord_path,
            missing=[],
            install_hint="",
        )

    # 3. Nothing available
    system = platform.system()
    if system == "Darwin":
        hint = "Install SoX: brew install sox"
    elif system == "Linux":
        hint = "Install SoX: sudo apt install sox  OR  sudo apt install alsa-utils"
    else:
        hint = "Install SoX: https://sox.sourceforge.net/"

    return VoiceDependency(
        available=False,
        backend="none",
        binary_path="",
        missing=[
            "sox (rec)" if system == "Darwin" else "sox (rec) or alsa-utils (arecord)",
        ],
        install_hint=hint,
    )


class AudioCapture:
    """Captures raw PCM audio from the microphone via subprocess.

    Usage::

        cap = AudioCapture()
        await cap.start()
        # ... recording ...
        audio_data = await cap.stop()  # returns raw PCM bytes
    """

    def __init__(self) -> None:
        self._process: asyncio.subprocess.Process | None = None
        self._backend: str = "none"
        self._chunks: list[bytes] = []
        self._reader_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Start recording from the microphone."""
        deps = check_voice_dependencies()
        if not deps.available:
            msg = f"No audio capture backend. {deps.install_hint}"
            raise RuntimeError(msg)

        self._backend = deps.backend
        self._chunks = []

        if self._backend == "sox":
            cmd = [
                deps.binary_path,
                "-q",  # quiet
                "--buffer",
                "1024",  # force frequent stdout flush
                "-t",
                "raw",  # raw PCM output
                "-r",
                str(SAMPLE_RATE),
                "-e",
                ENCODING,
                "-b",
                str(SAMPLE_WIDTH),
                "-c",
                str(CHANNELS),
                "-",  # stdout
            ]
        else:  # arecord
            cmd = [
                deps.binary_path,
                "-f",
                "S16_LE",
                "-r",
                str(SAMPLE_RATE),
                "-c",
                str(CHANNELS),
                "-t",
                "raw",
                "-q",
                "-",
            ]

        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )

        # Background reader to collect audio chunks.
        self._reader_task = asyncio.create_task(self._read_chunks())
        logger.debug("Audio capture started (backend=%s)", self._backend)

    async def _read_chunks(self) -> None:
        """Read audio data from subprocess stdout."""
        assert self._process is not None
        assert self._process.stdout is not None
        try:
            while True:
                chunk = await self._process.stdout.read(4096)
                if not chunk:
                    break
                self._chunks.append(chunk)
        except asyncio.CancelledError:
            logger.debug("suppressed exception in _read_chunks", exc_info=True)

    async def stop(self) -> bytes:
        """Stop recording and return captured PCM audio bytes."""
        if self._process is None:
            return b""

        # Terminate the recording process.
        try:
            self._process.terminate()
            await asyncio.wait_for(self._process.wait(), timeout=2.0)
        except (TimeoutError, ProcessLookupError):
            logger.debug("suppressed exception in stop", exc_info=True)
            self._process.kill()
            await self._process.wait()

        # Cancel reader and collect remaining data.
        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task

        self._process = None
        self._reader_task = None

        audio = b"".join(self._chunks)
        self._chunks = []
        logger.debug(
            "Audio capture stopped: %d bytes (%.1fs)",
            len(audio),
            len(audio) / (SAMPLE_RATE * 2),
        )
        return audio

    @property
    def is_recording(self) -> bool:
        return self._process is not None
