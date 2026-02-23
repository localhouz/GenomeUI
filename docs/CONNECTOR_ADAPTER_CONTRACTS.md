# Connector Adapter Contracts

This document defines the contract boundary between the intent/policy runtime and concrete connector providers.

Runtime source of truth:
- `GET /api/connectors/contracts`
- backend constant: `CONNECTOR_ADAPTER_CONTRACTS` in `backend/main.py`

## Design Goals
- Keep intent parsing and policy logic stable while adapters change.
- Allow `scaffold`, `mock`, and `live` implementations per provider.
- Enforce predictable response shapes for replay/UI rendering.

## Providers
- `weather`
- `banking`
- `social`
- `telephony`

## Operation Contract Shape
Each operation contract includes:
- `request`: required input fields and type expectations
- `response`: normalized output fields

Response conventions:
- `ok`: operation success boolean
- `source`: adapter mode/source string
- `error`: optional failure description for graceful UX fallback

## Current Operations
- `weather_forecast`
- `banking_balance_read`
- `banking_transactions_read`
- `social_feed_read`
- `social_message_send`
- `telephony_call_start`

## Compatibility Rule
If a live provider cannot satisfy an operation, adapters must return:
- `ok: false`
- stable `source: live`
- explicit `error`

This preserves deterministic policy handling and keeps the operator surface actionable.
