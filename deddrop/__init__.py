"""DedDrop — a silent, passive telemetry dead drop for WDGWars (wdgwars.pl).

Ingests airborne ADS-B aircraft feeds and MeshMapper LoRa wardriving telemetry,
accumulates seen signals, and flushes HMAC-signed data batches to WDGWars.
"""

__all__ = ["TOOL_NAME", "TOOL_VERSION"]

TOOL_NAME = "deddrop"
TOOL_VERSION = "1.3.0"
