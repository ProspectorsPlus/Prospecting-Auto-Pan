# Private analytics — no hosting (Discord webhook)

Each macro run gets posted to a **private Discord channel only you can see**. No
website, no server to run.

## One-time setup (≈2 min)
1. In Discord, make a **new server** for yourself (the `+` on the left → Create My
   Own). Don't invite anyone — it's just for you.
2. Make a channel (e.g. `#macro-usage`).
3. Channel → **Edit Channel → Integrations → Webhooks → New Webhook → Copy
   Webhook URL**.
4. Open the **`prospecting_config.json`** you ship inside the build and add:
   ```json
   "ANALYTICS_WEBHOOK": "PASTE_THE_WEBHOOK_URL_HERE",
   ```
   (One line, with the comma. That config is bundled into the app, so every user's
   macro reports to your private channel.)

## Viewing
Just open your `#macro-usage` channel. Every launch shows a card with:
- **User** (the Discord name they typed, if any)
- **Version**
- **IP** + **Location** + **ISP**
- **Access code (hash)** — match the hash to `ACCESS_CODES_PRIVATE.txt` to see
  which code they used

## Notes
- Only members of that private server (you) can see the channel.
- The webhook URL is baked into the build, so anyone who digs into the files
  could find and spam it. If that ever happens, delete the webhook in Discord and
  make a new one (update the config).
- Tracking only — to cut someone off, revoke/rotate their access code.
- The old self-hosted `analytics-server/` folder is no longer needed; you can
  delete it.
