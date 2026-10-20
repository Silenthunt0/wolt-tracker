# Wolt order tracker for Home Assistant

Polls Wolt's unofficial consumer API (the same calls the wolt.com
order-tracking page makes) and derives a **"courier is close"** signal from
the courier's live GPS position. Wolt exposes no such flag on the web; the
app's push comes from a server-side rule that cannot be subscribed to.

## Install (HACS)

1. HACS → ⋮ → **Custom repositories** → add
   `https://github.com/Silenthunt0/wolt-tracker`, category *Integration*.
2. Install **Wolt order tracker**, restart Home Assistant.
3. Add the YAML below, restart again. There is no UI config flow.

```yaml
# secrets.yaml
wolt_refresh_token: "<token from a dedicated wolt.com login, see below>"

# configuration.yaml
wolt_tracker:
  refresh_token: !secret wolt_refresh_token
  close_distance_m: 400   # metres from the drop-off address (default 400)
  close_eta_minutes: 3    # or when Wolt's ETA is this close (default 3)
  # idle_interval: 60     # s between orders-list polls when nothing is active
  # active_interval: 30   # s while the order is being prepared
  # courier_interval: 10  # s while the courier is en route (wolt.com uses 5)
```

Add the secret **before** restarting with the new block: a missing `!secret`
makes the whole configuration invalid.

## Entities (device "Wolt")

| Entity | Meaning |
| --- | --- |
| `sensor.wolt_order_status` | `idle` or Wolt's `received` / `acknowledged` / `production` / `ready` / `delivered` / `rejected`; attributes carry venue, ETA window, purchase id |
| `sensor.wolt_courier_distance` | metres from courier to drop-off; `latitude` / `longitude` attributes let a map card show the courier |
| `sensor.wolt_courier_eta` | Wolt's current ETA (timestamp) |
| `binary_sensor.wolt_courier_close` | on once status is `ready`, the courier is delivering *your* order, and distance ≤ `close_distance_m` or ETA ≤ `close_eta_minutes` (and in the future); latched until the order is delivered |
| `binary_sensor.wolt_order_active` | an order is in progress |
| `binary_sensor.wolt_auth_problem` | refresh token rejected; a persistent notification is raised too |

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
4. Put it in `secrets.yaml`, restart. Rotated tokens are persisted in
   `.storage/wolt_tracker.tokens`; the secret is re-read only when it changes.
5. If `binary_sensor.wolt_auth_problem` turns on, repeat 1–3 and either edit
   the secret and restart, or call the `wolt_tracker.set_refresh_token`
   action with the new token (no restart needed).

Your normal browser and phone Wolt sessions are separate logins and are not
affected.

## Caveats

Unofficial API: field names may change without notice. Each poll validates
the payload shape and raises a Home Assistant notification instead of
failing silently. Polling stays well below what wolt.com's own page does.
