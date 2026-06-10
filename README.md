# Tailscale Key Expiry Monitor

Polls the Tailscale API and posts a Discord webhook alert **only when a device
crosses a threshold**: 7 days left → 1 day left → expired. A green recovery
message is sent when a key is renewed. State persists across restarts, so you
never get duplicate alerts for the same tier.

Stdlib-only Python on Alpine. Image ~55MB, idle RSS ~12–18MB, capped at 64MB
in the compose file.

## Setup

1. **Tailscale OAuth client** (don't use an API access token — those expire in
   ≤90 days): [admin console → Settings → OAuth clients](https://login.tailscale.com/admin/settings/oauth)
   → Generate client with only the **Devices → Core → Read** scope.
2. **Discord webhook**: Server Settings → Integrations → Webhooks → New
   Webhook → Copy URL.
3. Configure and run:

```bash
cp .env.example .env   # paste in your three secrets
chmod 600 .env
docker compose up -d --build
docker compose logs -f # first cycle runs immediately
```

Run it from your home lab.
It only needs outbound HTTPS to api.tailscale.com and discord.com.

## Configuration (env vars)

| Variable | Default | Purpose |
|---|---|---|
| `TS_OAUTH_CLIENT_ID` / `TS_OAUTH_CLIENT_SECRET` | — | Tailscale OAuth client |
| `DISCORD_WEBHOOK_URL` | — | Where alerts go |
| `WARN_DAYS` | `7` | First threshold |
| `CRIT_DAYS` | `1` | Second threshold |
| `CHECK_INTERVAL_HOURS` | `6` | Poll frequency |
| `SEND_RECOVERY` | `true` | Green "key renewed" message |
| `DISCORD_MENTION` | (empty) | e.g. `<@your_user_id>` — pings you on CRITICAL/EXPIRED only |
| `TAILNET` | `-` | Tailnet name; `-` = OAuth client's default |
| `RUN_ONCE` | `false` | Single check then exit (for cron-style use) |
| `STATE_FILE` | `/data/state.json` | Persisted alert state |

Devices with key expiry disabled (e.g. tagged servers) are skipped automatically.

## Behavior details

- Alerts fire only when a device moves to a *worse* tier. Staying at 5 days
  remaining across many cycles sends nothing after the initial 7-day alert.
- If an admin extends a key (tier improves but isn't OK), the tracked tier is
  lowered silently so a future re-cross alerts again.
- Multiple crossings in one cycle are batched into one Discord message
  (up to 10 embeds).
- Devices removed from the tailnet are pruned from state.

## Test

```bash
python3 test_monitor.py   # offline, mocks the API and webhook
```
