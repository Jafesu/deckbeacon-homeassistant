"""Constants for the DeckBeacon integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "deckbeacon"
INTEGRATION_VERSION: Final = "0.1.0"
PROTOCOL_VERSION: Final = 1

PATH_PAIR: Final = "/api/deckbeacon/v1/pair"
PATH_SESSION: Final = "/api/deckbeacon/v1/ws"

PAIRING_WINDOW_SECONDS: Final = 600
HANDSHAKE_TIMEOUT: Final = 10.0
HELLO_TIMEOUT: Final = 10.0
HEARTBEAT_SECONDS: Final = 15
MAX_FRAME: Final = 65535 + 1024
MAX_PREAUTH_PER_IP: Final = 4
MAX_PREAUTH_TOTAL: Final = 64

CONF_DEVICE_ID: Final = "device_id"
CONF_DEVICE_STATIC: Final = "device_static"
CONF_NAME: Final = "name"
CONF_MODEL: Final = "model"
CONF_VENDOR: Final = "vendor"
CONF_METRIC_KEYS: Final = "metric_keys"

EVENT_DEVICE: Final = "deckbeacon_event"
SIGNAL_UPDATE: Final = "deckbeacon_update_{}"
SIGNAL_NEW_METRIC: Final = "deckbeacon_new_metric_{}"

# After a live sleep/shutdown announcement the device is expected to vanish
# (NetworkManager may drop Wi-Fi before the agent can close the session, as
# observed on the Dell G15): stop waiting for heartbeats after this grace period.
EXPECTED_DROP_GRACE: Final = 5.0

# Options: permission tiers granted to the device per transport (agent enforces too).
CONF_GM_LOCAL: Final = "game_management_local"
CONF_GM_REMOTE: Final = "game_management_remote"

COMMAND_ACK_TIMEOUT: Final = 15.0
TERMINAL_STATES: Final = frozenset({"dispatched", "confirmed", "rejected", "failed", "expired", "duplicate", "deferred"})
