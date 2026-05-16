"""WhatsApp via wuzapi sidecar (a Go REST wrapper around whatsmeow).

Free, personal-account WhatsApp integration that runs as a separate process
(``wuzapi`` Go binary). Obscura's adapter talks to it over loopback HTTP.

Dormant unless ``[messaging.whatsapp] transport = "wuzapi"`` and
``enabled = true`` are set in config.toml. Importing this package has zero
side effects.

Public surface is re-exported here once the modules below land:

* :class:`obscura.integrations.whatsapp.wuzapi.client.WuzapiClient`
* :class:`obscura.integrations.whatsapp.wuzapi.adapter.WuzapiAdapter`
"""

from __future__ import annotations
