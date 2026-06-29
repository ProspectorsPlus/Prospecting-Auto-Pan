# Discord bot — screenshot attachment

The macro now sends an optional screenshot with these events: `safe_stop`,
`recovery`, `stop` (hard-stop) and `stats`. It is added to the **same JSON POST**
to `/notify` that the bot already receives, as two extra fields:

```jsonc
{
  "username": "Prospectors Plus",
  "content": "⚠️ Safe-paused: ...",
  "event": "safe_stop",
  "user": "Tedtheidot",
  "stats": { ... },
  "screenshot": "<base64 PNG, no data: prefix>",   // NEW (only on some events)
  "screenshot_format": "png"                          // NEW
}
```

`screenshot` is a base64-encoded PNG (already downscaled to ~1280px wide on the
macro side, typically 200–600 KB). It is only present when the user has
"Attach a screenshot to alerts" enabled.

Below are drop-in handlers for the two most likely bot setups. Add the decode +
attach where you currently build/send the Discord message for `/notify`.

---

## A) discord.js (v14) — DM or channel send

```js
const { AttachmentBuilder } = require("discord.js");

// inside your /notify handler, after you've parsed `body` and resolved the
// target (a User for DMs, or a TextChannel):
async function sendNotify(target, body) {
  const files = [];
  if (body.screenshot) {
    const buf = Buffer.from(body.screenshot, "base64");
    // Discord rejects empty/oversized files; cap at ~8 MB for normal servers.
    if (buf.length > 0 && buf.length < 8 * 1024 * 1024) {
      const ext = body.screenshot_format || "png";
      files.push(new AttachmentBuilder(buf, { name: `prospectors.${ext}` }));
    }
  }
  await target.send({
    content: body.content || "(no message)",
    files,                              // empty array = no attachment
  });
}
```

If you use embeds, attach the same file and reference it:

```js
const embed = new EmbedBuilder().setTitle(body.event).setDescription(body.content);
if (files.length) embed.setImage("attachment://prospectors.png");
await target.send({ embeds: [embed], files });
```

---

## B) Raw Discord webhook (multipart) — no discord.js

If the bot just forwards to a Discord **webhook URL**, send `multipart/form-data`
with the file as `files[0]` and the text in `payload_json`:

```js
// Node 18+ has global fetch / FormData / Blob
async function forwardToWebhook(webhookUrl, body) {
  const form = new FormData();
  form.append(
    "payload_json",
    JSON.stringify({ username: body.username || "Prospectors Plus", content: body.content })
  );
  if (body.screenshot) {
    const buf = Buffer.from(body.screenshot, "base64");
    if (buf.length > 0 && buf.length < 8 * 1024 * 1024) {
      form.append("files[0]", new Blob([buf], { type: "image/png" }), "prospectors.png");
    }
  }
  await fetch(webhookUrl, { method: "POST", body: form });
}
```

---

## Notes

- **Keep it optional.** Always guard on `if (body.screenshot)` so old macro
  versions (no screenshot field) still work.
- **Size guard.** The `buf.length < 8 MB` check avoids Discord 413s. The macro
  already downscales, but a 4K multi-monitor grab can still be large; if you see
  rejects, lower `SHOT_TARGET_W` in the macro's Notifications/config.
- **Privacy.** The screenshot is the user's whole primary monitor. It only sends
  when they tick "Attach a screenshot to alerts", and only to their own DM.
- **Secret.** The existing `x-macro-secret` header is unchanged — keep validating
  it before processing the screenshot.
