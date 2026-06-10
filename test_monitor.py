"""Offline test: simulate a device walking toward expiry and verify alerts
fire only on tier crossings, recovery resets state, and disabled-expiry
devices are ignored."""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

os.environ.update(
    TS_OAUTH_CLIENT_ID="x", TS_OAUTH_CLIENT_SECRET="x",
    DISCORD_WEBHOOK_URL="http://example.invalid/webhook",
    STATE_FILE="/tmp/test_state.json", RUN_ONCE="true",
)
if os.path.exists("/tmp/test_state.json"):
    os.remove("/tmp/test_state.json")

import monitor

sent = []
monitor.get_access_token = lambda: "fake-token"
monitor.send_discord = lambda payload: sent.append(payload)

NOW = datetime.now(timezone.utc)

def dev(node_id, name, days_from_now, disabled=False):
    return {
        "nodeId": node_id, "name": name, "hostname": name.split(".")[0],
        "os": "linux", "keyExpiryDisabled": disabled,
        "expires": (NOW + timedelta(days=days_from_now)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

def run_cycle(devices):
    sent.clear()
    monitor.get_devices = lambda token: devices
    state = monitor.load_state()
    state = monitor.check_once(state)
    monitor.save_state(state)
    return [e["title"] for p in sent for e in p.get("embeds", [])], state

failures = []
def check(label, cond):
    print(("PASS" if cond else "FAIL"), "-", label)
    if not cond:
        failures.append(label)

# Cycle 1: racknerd at 30d (OK), laptop disabled, phone at 5d -> WARNING alert for phone only
titles, state = run_cycle([
    dev("n1", "racknerd.tail1234.ts.net", 30),
    dev("n2", "laptop.tail1234.ts.net", 3, disabled=True),
    dev("n3", "phone.tail1234.ts.net", 5),
])
check("cycle1: exactly one alert", len(titles) == 1)
check("cycle1: phone WARNING", titles and "phone" in titles[0] and "WARNING" in titles[0])
check("cycle1: disabled device not tracked", "n2" not in state)

# Cycle 2: same data -> no new alerts (same tier)
titles, state = run_cycle([
    dev("n1", "racknerd.tail1234.ts.net", 30),
    dev("n3", "phone.tail1234.ts.net", 4.5),
])
check("cycle2: no repeat alert in same tier", len(titles) == 0)

# Cycle 3: phone hits 0.5d -> CRITICAL crossing; racknerd drops to 6d -> WARNING crossing
titles, state = run_cycle([
    dev("n1", "racknerd.tail1234.ts.net", 6),
    dev("n3", "phone.tail1234.ts.net", 0.5),
])
check("cycle3: two alerts", len(titles) == 2)
check("cycle3: phone CRITICAL", any("phone" in t and "CRITICAL" in t for t in titles))
check("cycle3: racknerd WARNING", any("racknerd" in t and "WARNING" in t for t in titles))

# Cycle 4: phone expires -> EXPIRED crossing with mention; racknerd unchanged
os.environ["DISCORD_MENTION"] = "<@42>"
monitor.DISCORD_MENTION = "<@42>"
titles, state = run_cycle([
    dev("n1", "racknerd.tail1234.ts.net", 5.8),
    dev("n3", "phone.tail1234.ts.net", -0.2),
])
check("cycle4: one alert", len(titles) == 1)
check("cycle4: phone EXPIRED", "EXPIRED" in titles[0])
check("cycle4: mention attached", sent and sent[0].get("content") == "<@42>")

# Cycle 5: phone re-authed (180d) -> recovery message, state cleared
titles, state = run_cycle([
    dev("n1", "racknerd.tail1234.ts.net", 5.8),
    dev("n3", "phone.tail1234.ts.net", 180),
])
check("cycle5: recovery sent", len(titles) == 1 and "renewed" in titles[0])
check("cycle5: phone state cleared", "n3" not in state)

# Cycle 6: phone heads toward expiry again -> alerts fire again
titles, state = run_cycle([
    dev("n1", "racknerd.tail1234.ts.net", 5.8),
    dev("n3", "phone.tail1234.ts.net", 2),
])
check("cycle6: re-cross fires again", any("phone" in t and "WARNING" in t for t in titles))

# Cycle 7: device removed from tailnet -> state pruned
titles, state = run_cycle([dev("n1", "racknerd.tail1234.ts.net", 5.8)])
check("cycle7: departed device pruned", "n3" not in state)

print()
print("ALL TESTS PASSED" if not failures else f"{len(failures)} FAILURES: {failures}")
sys.exit(1 if failures else 0)
