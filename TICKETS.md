# GenomeUI — Build Ticket Board
> Last updated: 2026-03-13
> Track all stub/gap resolution work across backend ops, extractors, and scene renderers.

---

## Legend
- `[ ]` Pending
- `[~]` In Progress
- `[x]` Done
- **P1** — User-visible, high daily-use value
- **P2** — Important, moderate daily-use
- **P3** — Nice to have, wave-4 domains

---

## Wave A — Connector Completions
> OAuth flows + vault already wired. Just need API call handlers + scene renderers.

### [x] T-A01 — GitHub ops
**Priority**: P1
**Scope**: 5 backend ops, 3 extractors, 1 scene renderer (reuse `code` kind or new `github` kind)
**Ops**: `github_my_prs`, `github_pr_view`, `github_repo_search`, `github_issue_create`, `github_commit`
**Extractors**: `_ext_github_my_prs` (stub), `_ext_github_commit` (stub), `_ext_github_pr_view` (needs pr number extraction)
**API**: GitHub REST v3 — `/user/pulls`, `/repos/{owner}/{repo}/pulls/{n}`, `/search/repositories`, `/repos/{owner}/{repo}/issues`, `/repos/{owner}/{repo}/commits`
**Notes**: Token via `vault_retrieve("github")`. Add `github_status` op. Scene: PR list with status badges (open/merged/closed), commit log stream.

---

### [x] T-A02 — Jira ops
**Priority**: P1
**Scope**: 5 backend ops, 3 extractors, 1 scene renderer
**Ops**: `jira_my_issues`, `jira_sprint`, `jira_view`, `jira_create`, `jira_update`
**Extractors**: `_ext_jira_my_issues` (stub), `_ext_jira_sprint` (stub), `_ext_jira_view` (needs issue key extraction)
**API**: Jira REST v3 — `/myself/issues`, `/board/{id}/sprint`, `/issue/{key}`, `/issue` (POST), `/issue/{key}` (PUT)
**Notes**: Token via `vault_retrieve("jira")`. Add `jira_status` op. Scene: issue list with priority chips, sprint board view.

---

### [x] T-A03 — Notion ops
**Priority**: P1
**Scope**: 4 backend ops, 2 extractors, 1 scene renderer
**Ops**: `notion_find`, `notion_create`, `notion_database`, `notion_update`
**Extractors**: `_ext_notion_find` (needs query extraction), `_ext_notion_create` (needs title/content extraction)
**API**: Notion API v1 — `/search`, `/pages` (POST), `/databases/{id}/query`, `/pages/{id}` (PATCH)
**Notes**: Token via `vault_retrieve("notion")`. Add `notion_status` op. Scene: page list with last-edited timestamps, page content preview.

---

### [x] T-A04 — Asana ops
**Priority**: P1
**Scope**: 4 backend ops, 2 extractors, 1 scene renderer
**Ops**: `asana_my_tasks`, `asana_create`, `asana_project`, `asana_update`
**Extractors**: `_ext_asana_my_tasks` (stub), `_ext_asana_create` (needs task name/project extraction)
**API**: Asana REST v1 — `/tasks?assignee=me`, `/tasks` (POST), `/projects`, `/tasks/{gid}` (PUT)
**Notes**: Token via `vault_retrieve("asana")`. Add `asana_status` op. Scene: task list with due dates, project swimlanes.

---

### [x] T-A05 — Google Calendar ops
**Priority**: P1
**Scope**: 7 backend ops, 4 extractors, extend `calendar` scene renderer
**Ops**: `gcal_list`, `gcal_create`, `gcal_update`, `gcal_delete`, `calendar_list`, `calendar_create`, `calendar_cancel`
**Extractors**: `_ext_calendar_list`, `_ext_calendar_create`, `_ext_calendar_cancel`, `_ext_calendar_reschedule`
**API**: Google Calendar v3 — `/calendars/primary/events`, `/calendars/primary/events` (POST/PATCH/DELETE)
**Notes**: Token via `vault_retrieve("google_calendar")`. `calendar.*` ops should delegate to `gcal.*` when token present, scaffold otherwise. Scene: event stream with time blocks.

---

### [x] T-A06 — Google Drive ops
**Priority**: P1
**Scope**: 3 backend ops, 2 extractors, extend `document` scene renderer
**Ops**: `gdrive_list`, `gdrive_open`, `gdrive_create`, `gdrive_share`
**Extractors**: `_ext_gdrive_search` (needs filename/query extraction), `_ext_gdrive_create` (needs name/type extraction)
**API**: Google Drive v3 — `/files`, `/files/{id}`, `/files` (POST), `/files/{id}/permissions`
**Notes**: Token via `vault_retrieve("google_drive")`. Scene: file grid with type icons (doc/sheet/slide/folder), last-modified.

---

### [x] T-A07 — Slack ops (complete)
**Priority**: P1
**Scope**: 4 backend ops, 2 extractors, 1 scene renderer
**Ops**: `slack_read`, `slack_search`, `slack_status`, `slack_reaction`
**Extractors**: `_ext_slack_read` (needs channel extraction), `_ext_slack_search` (needs query extraction)
**API**: Slack Web API — `conversations.history`, `search.messages`, `users.profile.set`, `reactions.add`
**Notes**: Token via `vault_retrieve("slack")`. Extend existing `slack_send` handler. Scene: message thread stream with avatars, channel label.

---

### [x] T-A08 — Plaid / Banking ops
**Priority**: P2
**Scope**: 4 backend ops, 2 extractors, extend `banking` scene renderer
**Ops**: `banking_history`, `banking_pay`, `banking_statement`, `banking_transfer`
**Extractors**: `_ext_banking_balance` (stub — fix to pass through), `_ext_banking_transactions` (stub — fix)
**API**: Plaid — `/accounts/balance/get`, `/transactions/get`, `/payment_initiation/payment/create`
**Notes**: `banking_pay` and `banking_transfer` are high-risk — require confirm gate. Scene: extend bank-shell with transaction history list.

---

## Wave B — Daily-Use Domain Handlers

### [x] T-B01 — Health & Fitness ops
**Priority**: P1
**Scope**: 14 backend ops, 7 extractors, 1 scene renderer (already has canvas, needs HTML)
**Ops**: `health_steps`, `health_heart_rate`, `health_sleep`, `health_workout_log`, `health_workout_start`, `health_food_log`, `health_water`, `health_weight`, `health_goals`, `health_mood`, `health_medication`, `health_hrv`, `health_cycle`, `health_streak`
**Extractors**: `_ext_health_steps` (stub), `_ext_health_heart_rate` (stub), `_ext_health_sleep` (stub), `_ext_health_cycle` (stub), `_ext_health_streak` (stub), `_ext_health_goals` (stub), `_ext_health_hrv` (stub)
**Notes**: Scaffold mode is acceptable (no live health API yet). Scene: ring-based metric display (steps ring, heart rate pulse, sleep bars). Add `health_status` op.

---

### [x] T-B02 — Location ops
**Priority**: P1
**Scope**: 6 backend ops, 3 extractors, 1 scene renderer (location kind exists, extend it)
**Ops**: `location_directions`, `location_distance`, `location_nearby`, `location_saved`, `location_share`, `location_traffic`
**Extractors**: `_ext_location_status` (stub — fix), `_ext_location_share` (stub — fix), `_ext_location_directions` (needs origin/dest extraction)
**API**: Use `geolocation.py` (already in backend/). Scaffold fallback OK.
**Notes**: Scene: map-style dark surface with route line, waypoints, ETA chip.

---

### [x] T-B03 — Contacts ops
**Priority**: P1
**Scope**: 7 backend ops, 2 extractors, extend `contacts` scene renderer
**Ops**: `contacts_list`, `contacts_create`, `contacts_edit`, `contacts_delete`, `contacts_favorite`, `contacts_call`, `contacts_message`
**Extractors**: `_ext_contacts_list` (stub — fix), `_ext_contacts_create` (needs name/phone/email extraction)
**Notes**: `contacts_call` delegates to telephony. `contacts_message` delegates to messaging. Scene: extend existing contacts renderer with action buttons.

---

### [x] T-B04 — Email routing (`email.*` → `gmail.*`)
**Priority**: P1
**Scope**: 8 backend op aliases + extractors
**Ops**: `email_compose`, `email_read`, `email_reply`, `email_forward`, `email_archive`, `email_search`, `email_label`, `email_snooze`, `email_unsubscribe`
**Notes**: Gmail ops already implemented. `email.*` ops should delegate: check token → call `gmail_*` helper, else scaffold. This is mostly wiring, not new API work. Add extractors for `email_reply` (needs reply-to context), `email_label` (needs label name).

---

### [x] T-B05 — Notes extended ops
**Priority**: P2
**Scope**: 5 backend ops, 3 extractors
**Ops**: `note_edit`, `note_delete`, `note_pin`, `note_search`, `note_tag`
**Extractors**: `_ext_note_edit` (needs note name + content), `_ext_note_pin` (needs name), `_ext_note_search` (needs query)
**Notes**: All ops hit internal session graph. `note_delete` is medium risk. Scene: extend existing notes surface.

---

### [x] T-B06 — Reminders extended ops
**Priority**: P2
**Scope**: 3 backend ops, 2 extractors
**Ops**: `reminder_delete`, `reminder_recurring`, `reminder_snooze`
**Extractors**: `_ext_reminder_delete` (needs reminder ID/name), `_ext_reminder_recurring` (needs schedule expression)
**Notes**: All ops modify internal scheduler. Add to CAPABILITY_REGISTRY and whitelist.

---

## Wave C — Lifestyle Domains

### [x] T-C01 — Travel ops
**Priority**: P2
**Scope**: 8 backend ops, 4 extractors, extend `travel` scene renderer
**Ops**: `travel_flight_search`, `travel_flight_status`, `travel_hotel_search`, `travel_hotel_book`, `travel_checkin`, `travel_boarding_pass`, `travel_itinerary`, `travel_car_rental`
**Extractors**: `_ext_travel_boarding_pass` (stub — fix), `_ext_travel_flight_status` (needs flight number), `_ext_travel_hotel_search` (needs location/dates), `_ext_travel_checkin` (needs flight/hotel context)
**Notes**: Scaffold mode acceptable. Scene: flight card with route graphic, boarding pass surface with barcode placeholder.

---

### [x] T-C02 — Food Delivery ops
**Priority**: P2
**Scope**: 4 backend ops, 2 extractors, extend `food_delivery` scene renderer
**Ops**: `food_delivery_browse`, `food_delivery_order`, `food_delivery_reorder`, `food_delivery_track`
**Extractors**: `_ext_food_delivery_order` (needs restaurant/items), `_ext_food_delivery_track` (needs order ID)
**Notes**: Scaffold mode. Scene: order tracker with progress bar already exists — extend with ETA, driver, restaurant name.

---

### [x] T-C03 — Rideshare ops
**Priority**: P2
**Scope**: 4 backend ops, 2 extractors, extend `rideshare` scene renderer
**Ops**: `rideshare_book`, `rideshare_cancel`, `rideshare_schedule`, `rideshare_track`
**Extractors**: `_ext_rideshare_book` (needs destination, ride type), `_ext_rideshare_track` (needs ride ID)
**Notes**: Scaffold mode. Scene: destination + driver + ETA already exists — extend with ride type selector, cancel button.

---

### [x] T-C04 — Video Streaming ops
**Priority**: P2
**Scope**: 8 backend ops, 4 extractors, extend `video` scene renderer
**Ops**: `video_play`, `video_search`, `video_browse`, `video_continue`, `video_watchlist`, `video_recommend`, `video_cast`, `video_rate`
**Extractors**: `_ext_video_play` (needs title/service), `_ext_video_search` (needs query), `_ext_video_browse` (needs service/genre), `_ext_video_continue` (passthrough)
**Notes**: Scaffold mode. Scene: catalog grid (poster tiles), now-playing surface with progress bar.

---

### [x] T-C05 — Smart Home ops
**Priority**: P2
**Scope**: 8 backend ops, 4 extractors, extend `smarthome` scene renderer
**Ops**: `smarthome_lights`, `smarthome_thermostat`, `smarthome_lock`, `smarthome_camera`, `smarthome_scene`, `smarthome_energy`, `smarthome_appliance`
**Extractors**: `_ext_smarthome_lights` (needs device/brightness/color), `_ext_smarthome_thermostat` (needs temp/mode), `_ext_smarthome_lock` (needs device + confirm), `_ext_smarthome_scene` (needs scene name)
**Notes**: `smarthome_lock` is high-risk. Scene: device grid already exists — extend with live state values.

---

### [x] T-C06 — Music extended ops
**Priority**: P2
**Scope**: 5 backend ops (delegate to Spotify), 2 extractors
**Ops**: `music_play`, `music_like`, `music_queue`, `music_playlist_create`, `music_playlist_add`, `music_discover`, `music_radio`, `music_sleep_timer`, `music_cast`
**Notes**: These are `music.*` aliases for `spotify.*` ops. Wiring only — check token → delegate to spotify helper, else scaffold.

---

### [x] T-C07 — Phone ops
**Priority**: P2
**Scope**: 6 backend ops, 4 extractors
**Ops**: `phone_call`, `phone_recent`, `phone_voicemail`, `phone_block`, `phone_record`, `phone_conference`
**Extractors**: `_ext_phone_voicemail` (stub), `_ext_phone_recent` (stub), `_ext_phone_record` (stub), `_ext_phone_call` (needs number/contact)
**Notes**: `phone_call` delegates to `telephony_call_start`. Most ops are device-native scaffold.

---

### [x] T-C08 — Photos ops
**Priority**: P2
**Scope**: 4 backend ops, 2 extractors, extend `photos` scene renderer
**Ops**: `photos_search`, `photos_album`, `photos_edit`, `photos_share`
**Extractors**: `_ext_photos_search` (needs query/date), `_ext_photos_album` (needs album name)
**Notes**: Scaffold grid mode. Scene: 3-column memory grid already exists — add album nav, search overlay.

---

### [x] T-C09 — Messaging extended ops
**Priority**: P2
**Scope**: 9 backend ops, 4 extractors
**Ops**: `messaging_read`, `messaging_reply`, `messaging_react`, `messaging_forward`, `messaging_search`, `messaging_delete`, `messaging_block`, `messaging_schedule`, `messaging_group_create`, `messaging_group_add`
**Notes**: These work against the Genome transport layer (mesh). Most can delegate to existing session messaging infra.

---

## Wave D — Productivity Domains

### [x] T-D01 — Documents extended ops
**Priority**: P2
**Scope**: 6 backend ops, 3 extractors
**Ops**: `document_create`, `document_edit`, `document_delete`, `document_rename`, `document_export`, `document_share`, `document_template`
**Extractors**: `_ext_document_create` (needs name/template), `_ext_document_delete` (needs name, medium-risk), `_ext_document_export` (needs name + format)
**Notes**: All ops work against internal session graph (content model). High-risk ops need confirm gate.

---

### [x] T-D02 — Spreadsheet extended ops
**Priority**: P2
**Scope**: 6 backend ops, 3 extractors
**Ops**: `spreadsheet_create`, `spreadsheet_edit`, `spreadsheet_delete`, `spreadsheet_chart`, `spreadsheet_formula`, `spreadsheet_export`
**Notes**: Same pattern as documents. `spreadsheet_chart` needs chart-type extraction. Delegate to GSheets when token present.

---

### [x] T-D03 — Presentation extended ops
**Priority**: P2
**Scope**: 7 backend ops, 3 extractors
**Ops**: `presentation_create`, `presentation_edit`, `presentation_delete`, `presentation_export`, `presentation_share`, `presentation_speaker_notes`, `presentation_template`
**Notes**: Same pattern as documents. Delegate to Google Slides when token present.

---

### [x] T-D04 — Terminal extended ops
**Priority**: P2
**Scope**: 5 backend ops, 3 extractors
**Ops**: `terminal_history`, `terminal_kill`, `terminal_ssh`, `terminal_env`, `terminal_output`
**Extractors**: `_ext_terminal_history` (stub — fix), `_ext_terminal_output` (stub — fix), `_ext_terminal_ssh` (needs host/user extraction)
**Notes**: `terminal_kill` needs process name/PID. `terminal_ssh` opens an SSH session context. Scene: extend existing terminal surface.

---

### [x] T-D05 — Shopping extended ops
**Priority**: P2
**Scope**: 6 backend ops, 2 extractors
**Ops**: `shopping_cart`, `shopping_compare`, `shopping_orders`, `shopping_recommendations`, `shopping_track`, `shopping_wishlist`
**Extractors**: `_ext_shopping_cart` (stub — fix), `_ext_shopping_compare` (needs product names)
**Notes**: Scaffold mode. Scene: extend existing shopping surface with cart view, order tracker.

---

### [x] T-D06 — Files extended ops
**Priority**: P2
**Scope**: 5 backend ops, 2 extractors
**Ops**: `files_search`, `files_recent`, `files_download`, `files_upload`, `files_share`
**Extractors**: `_ext_files_recent` (stub — fix), `_ext_files_search` (needs query)
**Notes**: `files_list` and `files_read` already work. Extend scene renderer with recents rail.

---

### [x] T-D07 — Finance extended ops
**Priority**: P2
**Scope**: 4 backend ops, 2 extractors
**Ops**: `finance_portfolio`, `finance_watchlist`, `finance_news`, `finance_alert`
**Extractors**: `_ext_finance_portfolio` (stub — fix), `_ext_finance_watchlist` (needs ticker list)
**Notes**: Scaffold mode (no live brokerage API yet). Scene: portfolio summary with gain/loss row, watchlist tickers.

---

## Wave E — Wave-4 Domain Completions

### [x] T-E01 — Notifications system ops
**Priority**: P2
**Scope**: 5 backend ops, 3 extractors, 1 scene renderer
**Ops**: `notifications_view`, `notifications_clear`, `notifications_clear_app`, `notifications_mark_read`, `notifications_settings`
**Extractors**: `_ext_notif_view` (stub), `_ext_notif_clear` (stub), `_ext_notif_mark_read` (stub)
**Notes**: These are OS-level notification ops. Scaffold mode. Scene: notification center surface with grouped app rows.

---

### [x] T-E02 — Focus & Productivity ops
**Priority**: P2
**Scope**: 4 backend ops, 2 extractors
**Ops**: `focus_session`, `focus_pomodoro`, `focus_block`, `focus_stats`
**Extractors**: `_ext_focus_stats` (stub — fix), `_ext_focus_session` (needs duration)
**Notes**: `focus_session` and `focus_pomodoro` extend existing `timer_start`. Scene: extend focus surface with blocked apps list, session ring.

---

### [x] T-E03 — Wallet & Passes ops
**Priority**: P3
**Scope**: 4 backend ops, 2 extractors
**Ops**: `wallet_passes`, `wallet_loyalty`, `wallet_gift_card`, `wallet_coupon`
**Extractors**: `_ext_wallet_passes` (stub — fix), `_ext_wallet_loyalty` (needs store name)
**Notes**: Scaffold mode. Scene: card-wallet surface (passes as physical card renders).

---

### [x] T-E04 — VPN ops
**Priority**: P3
**Scope**: 3 backend ops, 1 extractor
**Ops**: `vpn_connect`, `vpn_disconnect`, `vpn_status`
**Extractors**: `_ext_vpn_status` (stub — fix)
**Notes**: Scaffold mode (no live VPN API). `vpn_connect` is medium-risk. Add to CAPABILITY_REGISTRY + whitelist.

---

### [x] T-E05 — Screen Capture ops
**Priority**: P3
**Scope**: 4 backend ops, 2 extractors
**Ops**: `screen_screenshot`, `screen_record`, `screen_mirror`, `screen_split`
**Extractors**: `_ext_screen_screenshot` (stub — fix), `_ext_screen_split` (stub — fix)
**Notes**: These are OS-level ops. Scaffold confirmations only — actual capture is out of backend scope.

---

### [x] T-E06 — Handoff & Continuity ops
**Priority**: P3
**Scope**: 4 backend ops, 2 extractors
**Ops**: `handoff_continue`, `handoff_clipboard`, `handoff_airdrop`, `handoff_screen_share`
**Extractors**: `_ext_handoff_clipboard` (stub — fix), `_ext_handoff_screen_share` (stub — fix)
**Notes**: These integrate with the Genome relay/mesh transport layer. `handoff_continue` routes to `genome_transport.ts`.

---

### [x] T-E07 — Backup ops
**Priority**: P3
**Scope**: 2 backend ops, 2 extractors
**Ops**: `backup_now`, `backup_status`
**Extractors**: `_ext_backup_now` (stub — fix), `_ext_backup_status` (stub — fix)
**Notes**: Scaffold mode. `backup_now` is medium-risk (starts a long operation). Add to CAPABILITY_REGISTRY + whitelist.

---

### [x] T-E08 — Password Manager ops
**Priority**: P3
**Scope**: 3 backend ops, 2 extractors
**Ops**: `password_find`, `password_generate`, `password_update`
**Notes**: `password_find` reads from `auth_vault.db` — already available via `vault_retrieve`. `password_generate` is pure computation. `password_update` is high-risk.

---

### [x] T-E09 — App Store ops
**Priority**: P3
**Scope**: 3 backend ops, 2 extractors
**Ops**: `app_find`, `app_install`, `app_update`
**Notes**: OS-level. Scaffold confirmations — actual install is out of backend scope.

---

### [x] T-E10 — Reading List ops
**Priority**: P3
**Scope**: 3 backend ops, 3 extractors
**Ops**: `reading_save`, `reading_list_view`, `reading_mark_read`
**Extractors**: `_ext_reading_save` (stub), `_ext_reading_list_view` (stub), `_ext_reading_mark_read` (stub)
**Notes**: Stored in session graph. Scene: reading list surface with article cards.

---

### [x] T-E11 — Accessibility & Settings ops
**Priority**: P3
**Scope**: 10 backend ops, 5 extractors
**Ops**: `access_display`, `access_font`, `access_voice`, `access_zoom`, `settings_airplane`, `settings_bluetooth`, `settings_brightness`, `settings_dnd`, `settings_wifi`
**Extractors**: `_ext_settings_battery` (stub), `_ext_settings_storage` (stub)
**Notes**: OS-level. Scaffold + confirm gate for airplane mode / bluetooth.

---

### [x] T-E12 — Shortcuts & Automations ops
**Priority**: P3
**Scope**: 3 backend ops, 2 extractors
**Ops**: `shortcut_list`, `shortcut_create`, `shortcut_run`
**Extractors**: `_ext_shortcut_list` (stub — fix), `_ext_shortcut_create` (needs trigger + action)
**Notes**: Map to internal intent scheduling / cron infra.

---

### [x] T-E13 — Dictionary & Reference ops
**Priority**: P3
**Scope**: 4 backend ops, 2 extractors
**Ops**: `dict_define`, `dict_etymology`, `dict_thesaurus`, `dict_wikipedia`
**Notes**: These can hit free APIs (Dictionary API, Wikipedia API) with no auth. High value, low effort.

---

### [x] T-E14 — Date Calculator ops
**Priority**: P3
**Scope**: 4 backend ops, 2 extractors
**Ops**: `date_age`, `date_countdown`, `date_day_of`, `date_days_until`
**Notes**: Pure computation, no external API. High reliability, low effort.

---

### [x] T-E15 — Alarm & Clock ops
**Priority**: P3
**Scope**: 4 backend ops, 1 extractor
**Ops**: `alarm_list`, `clock_stopwatch`, `clock_bedtime`, `clock_world`
**Extractors**: `_ext_alarm_list` (stub — fix)
**Notes**: `alarm_list` reads from session graph. Clock ops are computation + scene.

---

### [x] T-E16 — Podcasts ops
**Priority**: P3
**Scope**: 3 backend ops, 2 extractors
**Ops**: `podcast_find`, `podcast_queue`, `podcast_subscribe`
**Notes**: Scaffold mode (no live podcast API). Scene: extend existing `clock` kind or add `podcast` kind.

---

### [x] T-E17 — Recipes & Grocery ops
**Priority**: P3
**Scope**: 7 backend ops, 3 extractors
**Ops**: `recipe_find`, `recipe_save`, `recipe_scale`, `recipe_nutrition`, `grocery_list`, `grocery_add`, `grocery_order`
**Extractors**: `_ext_grocery_list` (stub — fix), `_ext_grocery_add` (needs item name), `_ext_recipe_nutrition` (needs recipe name)
**Notes**: Scaffold mode. Scene: recipe + grocery renderers already exist — just need backend handlers.

---

### [x] T-E18 — Books ops
**Priority**: P3
**Scope**: 4 backend ops, 2 extractors
**Ops**: `book_library`, `book_find`, `book_read`, `book_highlight`
**Extractors**: `_ext_book_library` (stub — fix), `_ext_book_find` (needs title/author)
**Notes**: Scaffold mode. Scene: book renderer already exists.

---

### [x] T-E19 — Translation ops
**Priority**: P3
**Scope**: 3 backend ops, 2 extractors
**Ops**: `translate_text`, `translate_detect`, `translate_conversation`
**Notes**: Can use a free translation API. Scene: two-pane translate renderer already exists.

---

### [x] T-E20 — Currency & Unit ops
**Priority**: P3
**Scope**: 3 backend ops, 2 extractors
**Ops**: `currency_rates`, `currency_convert`, `unit_convert`
**Extractors**: `_ext_currency_rates` (stub — fix), `_ext_unit_convert` (needs amount/from/to)
**Notes**: Free APIs (exchangerate-api.com). Pure computation for units. Low effort, high polish.

---

### [x] T-E21 — Payments ops
**Priority**: P3
**Scope**: 5 backend ops, 3 extractors
**Ops**: `payments_send`, `payments_request`, `payments_split`, `payments_history`, `payments_balance`
**Notes**: High-risk (send, split). Requires confirm gate. Scaffold mode until Plaid/Stripe connected.

---

## Wave F — Extractor Hardening

### [x] T-F01 — Fix core domain stub extractors
**Priority**: P1
**Scope**: Fix 14 critical extractors that return `{}` with no actual extraction
**Extractors**:
- `_ext_location_status` — extract city/address from utterance
- `_ext_banking_balance` — extract account type
- `_ext_banking_transactions` — extract date range, account
- `_ext_social_feed` — extract platform filter
- `_ext_social_notifications` — fine as-is (no params needed)
- `_ext_social_profile` — extract actor handle from utterance
- `_ext_terminal_history` — extract limit/filter
- `_ext_terminal_output` — extract session/command ref
- `_ext_finance_portfolio` — extract ticker/date range
- `_ext_contacts_list` — extract query filter
- `_ext_files_recent` — extract limit/filter

---

### [x] T-F02 — Fix health extractors
**Priority**: P2
**Scope**: 7 health/fitness extractors that return `{}`
**Extractors**: `_ext_health_steps`, `_ext_health_heart_rate`, `_ext_health_sleep`, `_ext_health_cycle`, `_ext_health_streak`, `_ext_health_goals`, `_ext_health_hrv`
**Notes**: Most can stay as `{}` (no params needed) — but should be annotated, not stubs. Some need date-range extraction.

---

### [x] T-F03 — Fix wave-4 stub extractors
**Priority**: P3
**Scope**: ~40 remaining stub extractors across wave-4 domains
**Extractors**: All `_ext_notif_*`, `_ext_handoff_*`, `_ext_shortcut_*`, `_ext_vpn_*`, `_ext_wallet_*`, `_ext_screen_*`, `_ext_backup_*`, `_ext_reading_*`, `_ext_currency_*`, `_ext_connections_manage`, `_ext_grocery_list`, `_ext_book_library`, `_ext_alarm_list`, `_ext_settings_battery`, `_ext_settings_storage`, `_ext_maps_share_eta`, `_ext_travel_boarding_pass`
**Notes**: Most of these ops have no params — returning `{}` is technically correct but they should be documented as intentional.

---

## Progress Summary

| Wave | Tickets | Done | In Progress | Pending |
|------|---------|------|-------------|---------|
| A — Connectors | 8 | 8 | 0 | 0 |
| B — Daily-Use | 6 | 6 | 0 | 0 |
| C — Lifestyle | 9 | 9 | 0 | 0 |
| D — Productivity | 7 | 7 | 0 | 0 |
| E — Wave-4 | 21 | 21 | 0 | 0 |
| F — Extractors | 3 | 3 | 0 | 0 |
| **Total** | **54** | **54** | **0** | **0** |

---

## Completed

> Move tickets here when done. Format: `[x] T-XXX — Title (completed YYYY-MM-DD)`

- [x] T-E13 — Dictionary & Reference (completed 2026-03-13)
- [x] T-E14 — Date Calculator ops (completed 2026-03-13)
- [x] T-E15 — Alarm & Clock ops (completed 2026-03-13)
- [x] T-E20 — Currency & Unit ops (completed 2026-03-13)
- [x] T-B01 — Health & Fitness ops (completed 2026-03-13)
- [x] T-A01 — GitHub ops (completed 2026-03-13)
- [x] T-A02 — Jira ops (completed 2026-03-13)
- [x] T-A03 — Notion ops (completed 2026-03-13)
- [x] T-A04 — Asana ops (completed 2026-03-13)
- [x] T-A05 — Google Calendar ops (completed 2026-03-13)
- [x] T-A06 — Google Drive ops (completed 2026-03-13)
- [x] T-A07 — Slack ops complete (completed 2026-03-13)
- [x] T-B02 — Location ops (completed 2026-03-13)
- [x] T-B03 — Contacts ops (completed 2026-03-13)
- [x] T-B04 — Email routing: email.* → gmail.* delegation (completed 2026-03-13)
- [x] T-F01 — Fix core domain stub extractors (completed 2026-03-13)
- [x] T-A08 — Plaid / Banking ops (completed 2026-03-13)
- [x] T-B05 — Notes extended ops (completed 2026-03-13)
- [x] T-B06 — Reminders extended ops (completed 2026-03-13)
- [x] T-C01 — Travel ops (completed 2026-03-13)
- [x] T-C02 — Food Delivery ops (completed 2026-03-13)
- [x] T-C03 — Rideshare ops (completed 2026-03-13)
- [x] T-C04 — Video Streaming ops (completed 2026-03-13)
- [x] T-C05 — Smart Home ops (completed 2026-03-13)
- [x] T-C06 — Music extended ops (completed 2026-03-13)
- [x] T-C07 — Phone ops (completed 2026-03-13)
- [x] T-C08 — Photos ops (completed 2026-03-13)
- [x] T-C09 — Messaging extended ops (completed 2026-03-13)
- [x] T-D01 — Documents extended ops (completed 2026-03-13)
- [x] T-D02 — Spreadsheet extended ops (completed 2026-03-13)
- [x] T-D03 — Presentation extended ops (completed 2026-03-13)
- [x] T-D04 — Terminal extended ops (completed 2026-03-13)
- [x] T-D05 — Shopping extended ops (completed 2026-03-13)
- [x] T-D06 — Files extended ops (completed 2026-03-13)
- [x] T-D07 — Finance extended ops (completed 2026-03-13)
- [x] T-E01 — Notifications system ops (completed 2026-03-13)
- [x] T-E02 — Focus & Productivity ops (completed 2026-03-13)
- [x] T-E03 — Wallet & Passes ops (completed 2026-03-13)
- [x] T-E04 — VPN ops (completed 2026-03-13)
- [x] T-E05 — Screen Capture ops (completed 2026-03-13)
- [x] T-E06 — Handoff & Continuity ops (completed 2026-03-13)
- [x] T-E07 — Backup ops (completed 2026-03-13)
- [x] T-E08 — Password Manager ops (completed 2026-03-13)
- [x] T-E09 — App Store ops (completed 2026-03-13)
- [x] T-E10 — Reading List ops (completed 2026-03-13)
- [x] T-E11 — Accessibility & Settings ops (completed 2026-03-13)
- [x] T-E12 — Shortcuts & Automations ops (completed 2026-03-13)
- [x] T-E16 — Podcasts ops (completed 2026-03-13)
- [x] T-E17 — Recipes & Grocery ops (completed 2026-03-13)
- [x] T-E18 — Books ops (completed 2026-03-13)
- [x] T-E19 — Translation ops (completed 2026-03-13)
- [x] T-E21 — Payments ops (completed 2026-03-13)
- [x] T-F02 — Fix health extractors (completed 2026-03-13)
- [x] T-F03 — Fix wave-4 stub extractors (completed 2026-03-13)
