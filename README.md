<p align="center"><img src="images/banner.png" alt="DeckBeacon" width="600"></p>

# DeckBeacon for Home Assistant

Home Assistant integration for **DeckBeacon**: local-first monitoring and management of SteamOS devices (Steam Deck and other SteamOS PCs). It shows system telemetry, power state, the Steam library and current game, live downloads and game artwork, and launches games, using native Home Assistant entities and actions.

> **Pre-release.** This repository is generated from the [DeckBeacon monorepo](https://github.com/Jafesu/DeckBeacon); please file issues and read the docs there.

## Requirements
- Home Assistant 2026.5.0 or newer.
- The DeckBeacon agent running on the SteamOS device (see the monorepo).
- Optional: the [DeckBeacon cards](https://github.com/Jafesu/deckbeacon-card) for the dashboard.

## Install
Marketplace → **Add from link** → `https://github.com/Jafesu/deckbeacon-homeassistant` (type: Integration). Restart Home Assistant, then pair the device from the agent with `deckbeacon pair`.

Version 0.3.0, built from monorepo commit `bf7e0b6`.
