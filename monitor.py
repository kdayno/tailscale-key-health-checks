#!/usr/bin/env python3
"""
Tailscale Key Expiry Monitor
----------------------------
Polls the Tailscale API for device key expiry and sends a Discord webhook
alert ONLY when a device crosses a threshold tier:

    OK  ->  WARNING (<= 7 days)  ->  CRITICAL (<= 1 day)  ->  EXPIRED

State is persisted to a JSON file so restarts don't re-alert. When a key is
renewed (device returns to OK), an optional recovery message is sent and the
device's state is reset so future expiry cycles alert again.

Uses only the Python standard library. Auth via Tailscale OAuth client
(client secrets don't expire, unlike API access tokens).
"""

import json
import logging
import os
import re
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# --------------------------------------------------------------------------
# Configuration (environment variables)
# --------------------------------------------------------------------------
TS_OAUTH_CLIENT_ID = os.environ.get("TS_OAUTH_CLIENT_ID", "")
TS_OAUTH_CLIENT_SECRET = os.environ.get("TS_OAUTH_CLIENT_SECRET", "")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

TAILNET = os.environ.get("TAILNET", "-")  # "-" = default tailnet for the OAuth client
WARN_DAYS = float(os.environ.get("WARN_DAYS", "7"))
CRIT_DAYS = float(os.environ.get("CRIT_DAYS", "1"))
CHECK_INTERVAL_HOURS = float(os.environ.get("CHECK_INTERVAL_HOURS", "6"))
STATE_FILE = os.environ.get("STATE_FILE", "/data/state.json")
SEND_RECOVERY = os.environ.get("SEND_RECOVERY", "true").lower() in ("1", "true", "yes")
RUN_ONCE = os.environ.get("RUN_ONCE", "false").lower() in ("1", "true", "yes")
DISCORD_MENTION = os.environ.get("DISCORD_MENTION", "")  # e.g. "<@123456789>" to ping on CRITICAL/EXPIRED

API_BASE = "https://api.tailscale.com/api/v2"
# Discord sits behind Cloudflare, which rejects urllib's default
# "Python-urllib/3.x" user agent with error 1010.
USER_AGENT = "tailscale-key-monitor/1.0 (+https://github.com/kdayno/tailscale-key-health-checks)"

# Tier ordering: higher = worse. Alerts fire only when tier number increases.
TIERS = {"OK": 0, "WARNING": 1, "CRITICAL": 2, "EXPIRED": 3}
TIER_COLORS = {
    "WARNING": 0xFFC107,   # yellow
    "CRITICAL": 0xFF6B00,  # orange
    "EXPIRED": 0xDC3545,   # red
    "RECOVERED": 0x28A745, # green
}
TIER_EMOJI = {"WARNING": "⚠️", "CRITICAL": "🚨", "EXPIRED": "💀", "RECOVERED": "✅"}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S%z",
)
log = logging.getLogger("ts-key-monitor")

_shutdown = False


def _handle_signal(signum, _frame):
    global _shutdown
    log.info("Received signal %s, shutting down after current cycle.", signum)
    _shutdown = True


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


# --------------------------------------------------------------------------
# HTTP helpers (stdlib only)
# --------------------------------------------------------------------------
def http_request(url, data=None, headers=None, method=None, timeout=30):
    headers = {"User-Agent": USER_AGENT, **(headers or {})}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read()


def get_access_token():
    """Exchange OAuth client credentials for a short-lived access token."""
    body = urllib.parse.urlencode(
        {
            "client_id": TS_OAUTH_CLIENT_ID,
            "client_secret": TS_OAUTH_CLIENT_SECRET,
            "grant_type": "client_credentials",
        }
    ).encode()
    status, raw = http_request(
        f"{API_BASE}/oauth/token",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    if status != 200:
        raise RuntimeError(f"OAuth token request failed with HTTP {status}")
    return json.loads(raw)["access_token"]


def get_devices(token):
    status, raw = http_request(
        f"{API_BASE}/tailnet/{urllib.parse.quote(TAILNET)}/devices",
        headers={"Authorization": f"Bearer {token}"},
    )
    if status != 200:
        raise RuntimeError(f"Device list request failed with HTTP {status}")
    return json.loads(raw).get("devices", [])


def send_discord(payload):
    body = json.dumps(payload).encode()
    status, _ = http_request(
        DISCORD_WEBHOOK_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    if status not in (200, 204):
        raise RuntimeError(f"Discord webhook returned HTTP {status}")


# --------------------------------------------------------------------------
# State persistence
# --------------------------------------------------------------------------
def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


# --------------------------------------------------------------------------
# Core logic
# --------------------------------------------------------------------------
def parse_expires(value):
    """Return an aware datetime, or None if the device has no meaningful expiry."""
    if not value or value.startswith("0001-"):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def tier_for(days_left):
    if days_left <= 0:
        return "EXPIRED"
    if days_left <= CRIT_DAYS:
        return "CRITICAL"
    if days_left <= WARN_DAYS:
        return "WARNING"
    return "OK"


def humanize(days_left):
    if days_left <= 0:
        return f"expired {abs(days_left):.1f} days ago" if days_left < -0.05 else "expired just now"
    if days_left < 1:
        return f"{days_left * 24:.0f} hours remaining"
    return f"{days_left:.1f} days remaining"


def build_embed(device, tier, days_left, expires_dt):
    name = device.get("name") or device.get("hostname") or device.get("nodeId", "unknown")
    short_name = name.split(".")[0]
    title_tier = "key has EXPIRED" if tier == "EXPIRED" else f"key expiring soon ({tier})"
    embed = {
        "title": f"{TIER_EMOJI[tier]} Tailscale: {short_name} — {title_tier}",
        "color": TIER_COLORS[tier],
        "fields": [
            {"name": "Device", "value": name, "inline": False},
            {"name": "Status", "value": humanize(days_left), "inline": True},
            {
                "name": "Expiry (UTC)",
                "value": expires_dt.strftime("%Y-%m-%d %H:%M"),
                "inline": True,
            },
            {"name": "OS", "value": device.get("os", "?"), "inline": True},
            {
                "name": "Fix",
                "value": "[Open admin console](https://login.tailscale.com/admin/machines) "
                         "or run `tailscale up --force-reauth` on the device.",
                "inline": False,
            },
        ],
        "footer": {"text": "tailscale-key-monitor"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    return embed


def build_recovery_embed(device):
    name = device.get("name") or device.get("hostname") or "unknown"
    return {
        "title": f"{TIER_EMOJI['RECOVERED']} Tailscale: {name.split('.')[0]} — key renewed",
        "description": f"`{name}` is back to a healthy expiry window.",
        "color": TIER_COLORS["RECOVERED"],
        "footer": {"text": "tailscale-key-monitor"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def check_once(state):
    token = get_access_token()
    devices = get_devices(token)
    now = datetime.now(timezone.utc)
    log.info("Fetched %d devices from tailnet '%s'.", len(devices), TAILNET)

    seen_ids = set()
    embeds_to_send = []
    mention_needed = False

    for device in devices:
        node_id = device.get("nodeId") or device.get("id")
        if not node_id:
            continue
        seen_ids.add(node_id)
        name = device.get("name", node_id)

        if device.get("keyExpiryDisabled"):
            state.pop(node_id, None)  # nothing to track
            continue

        expires_dt = parse_expires(device.get("expires", ""))
        if expires_dt is None:
            continue  # external/shared devices may have no expiry

        days_left = (expires_dt - now).total_seconds() / 86400.0
        tier = tier_for(days_left)
        prev = state.get(node_id, {})
        prev_tier = prev.get("tier", "OK")

        if TIERS[tier] > TIERS[prev_tier]:
            # Crossed into a worse tier -> alert.
            log.info("%s crossed %s -> %s (%s).", name, prev_tier, tier, humanize(days_left))
            embeds_to_send.append(build_embed(device, tier, days_left, expires_dt))
            if tier in ("CRITICAL", "EXPIRED"):
                mention_needed = True
            state[node_id] = {"tier": tier, "name": name, "expires": device.get("expires")}
        elif tier == "OK" and prev_tier != "OK":
            # Key was renewed -> reset state, optionally notify.
            log.info("%s recovered (%s -> OK).", name, prev_tier)
            if SEND_RECOVERY:
                embeds_to_send.append(build_recovery_embed(device))
            state.pop(node_id, None)
        else:
            # Same tier or improved-but-not-OK (e.g. expiry extended from
            # EXPIRED back to WARNING): update tracked tier without alerting,
            # so a future re-cross fires again.
            if prev and TIERS[tier] < TIERS[prev_tier]:
                state[node_id]["tier"] = tier

    # Drop state for devices removed from the tailnet.
    for stale in [k for k in state if k not in seen_ids]:
        log.info("Removing state for departed device %s.", state[stale].get("name", stale))
        state.pop(stale)

    # Discord allows up to 10 embeds per message.
    for i in range(0, len(embeds_to_send), 10):
        payload = {"embeds": embeds_to_send[i : i + 10]}
        if DISCORD_MENTION and mention_needed:
            payload["content"] = DISCORD_MENTION
        send_discord(payload)

    if embeds_to_send:
        log.info("Sent %d alert embed(s) to Discord.", len(embeds_to_send))
    else:
        log.info("No threshold crossings; nothing sent.")

    return state


def validate_config():
    missing = [
        n for n, v in [
            ("TS_OAUTH_CLIENT_ID", TS_OAUTH_CLIENT_ID),
            ("TS_OAUTH_CLIENT_SECRET", TS_OAUTH_CLIENT_SECRET),
            ("DISCORD_WEBHOOK_URL", DISCORD_WEBHOOK_URL),
        ] if not v
    ]
    if missing:
        log.error("Missing required environment variables: %s", ", ".join(missing))
        sys.exit(1)


def main():
    validate_config()
    log.info(
        "Starting: warn<=%gd crit<=%gd, interval=%gh, state=%s, recovery=%s",
        WARN_DAYS, CRIT_DAYS, CHECK_INTERVAL_HOURS, STATE_FILE, SEND_RECOVERY,
    )
    while not _shutdown:
        try:
            state = load_state()
            state = check_once(state)
            save_state(state)
        except urllib.error.HTTPError as e:
            # Never log the webhook URL — its path is a secret token.
            safe_url = re.sub(r"/webhooks/\S+", "/webhooks/<redacted>", e.url or "")
            log.error("HTTP %s from %s: %s", e.code, safe_url, e.read()[:300])
        except Exception:
            log.exception("Check cycle failed; will retry next interval.")

        if RUN_ONCE:
            break
        # Sleep in small slices so SIGTERM is handled promptly.
        deadline = time.monotonic() + CHECK_INTERVAL_HOURS * 3600
        while not _shutdown and time.monotonic() < deadline:
            time.sleep(min(5, max(0, deadline - time.monotonic())))

    log.info("Stopped.")


if __name__ == "__main__":
    main()
