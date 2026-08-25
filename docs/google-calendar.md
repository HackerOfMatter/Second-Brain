# Google Calendar and Google Tasks sync

Optional. The default `.ics` sink already gives you work blocks, recurring
areas, due tasks and reminders with no accounts involved — use this only when
you want them pushed into Google directly.

Two APIs are involved, because the system produces two different things:

- **Google Calendar** takes the *events* — project work blocks, recurring area
  blocks, review prompts.
- **Google Tasks** takes the *due dates*. A project deadline is a task, not an
  appointment, so it belongs in the task system; it then shows in the Tasks
  strip of Google Calendar on the day it is due and stays there until ticked.

They share one OAuth consent and one token file, so this is still a single
setup.

## The easy way first

`<vault>/_system/calendar/secondbrain.ics` is rebuilt after every change.

- **One-off import:** Google Calendar → Settings → Import & export → Import.
  Simple, but it does not update — re-importing creates duplicates.
- **Live subscription:** Outlook and Apple Calendar can subscribe to the local
  file path and stay current. Google Calendar can only subscribe to a public
  URL, which a privacy-first local system deliberately does not have. That's
  the gap the API sink fills.

## API setup (one time, ~10 minutes)

1. **Create a project** at <https://console.cloud.google.com/> — any name.
2. **Enable both APIs:** APIs & Services → Library → enable "Google Calendar
   API" *and* "Google Tasks API". Missing the second one is the most common
   way this setup half-works: events sync, due dates fail.
3. **Configure the consent screen:** External, fill in the required fields, and
   add your own Google account under *Test users*. You do not need to publish
   or get the app verified — a test user can use it indefinitely.
4. **Create credentials:** Credentials → Create credentials → OAuth client ID →
   **Desktop app**. Download the JSON.
5. **Install it:** save the file as
   `<vault>/_system/credentials.json`.
6. **Install the libraries:**

   ```powershell
   pip install google-api-python-client google-auth-oauthlib
   ```

7. **Switch the sink** in `config.yaml`:

   ```yaml
   calendar:
     sink: google              # or: both — events
     task_sink: auto           # follows `sink`; set explicitly to split them
     google_calendar_id: primary
     google_tasklist: Second Brain   # created on first sync
   ```

8. **Authorise:** `python run.py sync`. A browser window opens once; after you
   approve, the token is written to `<vault>/_system/token.json` and refreshed
   automatically from then on.

## Notes

- `token.json` is a credential. `_system/.gitignore` already excludes it — keep
  it that way if you version your vault.
- A dedicated calendar is easier to live with than `primary`: create one in
  Google Calendar, open its settings, copy the *Calendar ID*
  (`...@group.calendar.google.com`) into `google_calendar_id`. You can then
  toggle all Second Brain events on and off with one checkbox.
- Sync is an upsert keyed on a stable event id, so re-running it updates
  existing events and removes ones whose step is done or deleted. It only ever
  touches events it created — they carry a private `sb_kind` property.
- Tasks work the same way, but Google assigns task ids, so each task carries an
  `[sb:…]` marker in its notes and the sink finds its own work by reading it
  back. A task you added by hand has no marker and is never touched.
- **If you set this up before due dates became tasks:** the token you already
  have was granted calendar-only and cannot create tasks. Delete
  `<vault>/_system/token.json` and run `python run.py sync` once to re-consent
  with both scopes. The first sync also deletes the old all-day `DUE:` banners
  from your calendar, since the vault no longer asks for them.
- Areas sync as *recurring* events (one series, not one event per occurrence).
  Google needs a named zone for a series, so set `calendar.timezone` if the
  machine's own zone name is not what you want.
- Colour: events get Google's `colorId` from the note's category. Google Tasks
  has no colour API — the category emoji in the task title is the only signal
  there, which is why `calendar.emoji_prefix` defaults to on.
- Events you drag to a new time in Google Calendar will be moved back on the
  next sync. The vault is the source of truth; move the block by pressing
  **Re-plan** in the dashboard instead.
- `sink: both` writes the `.ics` and pushes to Google. If Google fails, the
  `.ics` still updates and the error is logged to `_system/logs/calendar.log`.
