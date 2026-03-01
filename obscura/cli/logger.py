# Central CLI logger configuration for obscura
from __future__ import annotations

import logging
from typing import Optional

from obscura.cli import render


def configure_logger(output_manager: Optional[object] = None) -> logging.Logger:
    """Configure a central 'obscura' logger that routes INFO+ to the user-facing
    console (or output manager when not in CLI) and routes DEBUG messages into
    the OutputManager.capture_internal buffer.
    Safe to call multiple times.
    """
    out = output_manager or render.output

    logger = logging.getLogger("obscura")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    # clear existing handlers to make configure idempotent
    for h in list(logger.handlers):
        logger.removeHandler(h)

    fmt = logging.Formatter("%(levelname)s: %(message)s")

    class InfoHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if record.levelno < logging.INFO:
                return
            msg = self.format(record)
            try:
                if getattr(out, "env", "cli") == "cli":
                    # user-facing console output
                    render.console.print(msg)
                else:
                    out.capture_internal(msg)
            except Exception:
                pass

    class DebugHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if record.levelno != logging.DEBUG:
                return
            msg = self.format(record)
            try:
                # Always capture debug messages to the internal buffer
                out.capture_internal(msg)
            except Exception:
                pass

    ih = InfoHandler()
    ih.setLevel(logging.INFO)
    ih.setFormatter(fmt)

    dh = DebugHandler()
    dh.setLevel(logging.DEBUG)
    dh.setFormatter(fmt)

    logger.addHandler(ih)
    logger.addHandler(dh)

    return logger


# Auto-configure on import using the global OutputManager
configure_logger()
