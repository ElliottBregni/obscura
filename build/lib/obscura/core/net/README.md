Core networking helpers

This module provides small, framework-agnostic HTTP and WebSocket helpers:

- makeDefaultHttpClient() - lightweight fetch-based client with tokenProvider support
- WsClient - small WebSocket wrapper supporting tokenProvider and callbacks
- TokenProvider helpers and retry utility

Notes
- Uses global fetch / WebSocket when available; falls back to 'ws' in Node.
- Keep this module minimal; platform-specific integrations should adapt via adapters.
