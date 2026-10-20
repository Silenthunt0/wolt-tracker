# Wolt order tracker for Home Assistant

Polls Wolt's unofficial consumer API (the same calls the wolt.com
order-tracking page makes) and derives a **"courier is close"** signal from
the courier's live GPS position. Wolt exposes no such flag on the web; the
app's push comes from a server-side rule that cannot be subscribed to.

## Install (HACS)

1. HACS → ⋮ → **Custom repositories** → add
   `https://github.com/Silenthunt0/wolt-tracker`, category *Integration*.
2. Install **Wolt order tracker**, restart Home Assistant.
3. Settings → Devices & services → **Add integration** → *Wolt order tracker*
   and paste a refresh token (see [The token](#the-token-the-one-fragile-part)).
   The dialog does one real token refresh, so a bad token is rejected on the spot.
4. Thresholds and poll rates live under the integration's **Configure** button:
   close distance (default 400 m), close ETA (default 3 min), and the three
   poll intervals (60 s idle, 30 s while preparing, 10 s with the courier en
   route; wolt.com itself polls every 5 s).

No YAML. If Wolt ever rejects the stored token, Home Assistant shows its
standard *Re-authentication required* prompt on the integration; paste a fresh
token there.

## Entities (device "Wolt")

| Entity | Meaning |
| --- | --- |
| `sensor.wolt_order_status` | `idle` or Wolt's `received` / `acknowledged` / `production` / `ready` / `delivered` / `rejected`; attributes carry venue, ETA window, purchase id |
| `sensor.wolt_courier_distance` | metres from courier to drop-off; `latitude` / `longitude` attributes let a map card show the courier |
| `sensor.wolt_courier_eta` | Wolt's current ETA (timestamp) |
| `binary_sensor.wolt_courier_close` | on once status is `ready`, the courier is delivering *your* order, and distance ≤ `close_distance_m` or ETA ≤ `close_eta_minutes` (and in the future); latched until the order is delivered |
| `binary_sensor.wolt_order_active` | an order is in progress |

Only the newest in-progress order is followed.

Example automation:

```yaml
triggers:
  - trigger: state
    entity_id: binary_sensor.wolt_courier_close
    to: "on"
actions:
  - action: notify.mobile_app_phone
    data:
      title: Wolt courier close
      message: >-
        {{ state_attr('binary_sensor.wolt_courier_close', 'distance_m') }} m away
```

## The token (the one fragile part)

Wolt access tokens live 30 min; the integration refreshes them with a refresh
token via `authentication.wolt.com/v1/wauth2/access_token`. **Refresh tokens
rotate on every use and only the newest chain survives**: using an older
token again revokes the one issued after it. So Home Assistant must own a
login nobody else refreshes:

1. Open a **private/incognito window**, log in at wolt.com.
2. DevTools → Application → Cookies → `https://wolt.com` → copy the refresh
   token value (bare string, without the URL-encoded quotes).
3. **Close the private window** and never use that session again.
4. Paste it into the integration dialog. Every rotated token is written back
   to the config entry immediately, so a restart never loses the chain.
5. If Wolt rejects the token later, the integration shows HA's standard
   *Re-authentication required* prompt: repeat 1–3 and paste the new value.

Your normal browser and phone Wolt sessions are separate logins and are not
affected.

## Caveats

Unofficial API: field names may change without notice. Each poll validates
the payload shape and surfaces failures through the entity availability and
HA's re-authentication prompt instead of failing silently. Polling stays well below what wolt.com's own page does.
