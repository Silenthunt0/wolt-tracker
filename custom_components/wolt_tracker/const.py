"""Constants for the Wolt order tracker."""

from datetime import timedelta

DOMAIN = "wolt_tracker"

# Config entry data
CONF_REFRESH_TOKEN = "refresh_token"
CONF_ACCESS_TOKEN = "access_token"
CONF_EXPIRES_AT = "expires_at"
CONF_USER_ID = "user_id"

# Options
CONF_CLOSE_DISTANCE = "close_distance_m"
CONF_CLOSE_ETA = "close_eta_minutes"
CONF_IDLE_INTERVAL = "idle_interval"
CONF_ACTIVE_INTERVAL = "active_interval"
CONF_COURIER_INTERVAL = "courier_interval"

DEFAULT_CLOSE_DISTANCE = 400  # metres
DEFAULT_CLOSE_ETA = 3  # minutes
DEFAULT_IDLE_INTERVAL = 60  # s, no order in progress
DEFAULT_ACTIVE_INTERVAL = 30  # s, order in progress, courier not yet en route
DEFAULT_COURIER_INTERVAL = 10  # s, courier en route (Wolt's own page uses 5)

AUTH_URL = "https://authentication.wolt.com/v1/wauth2/access_token"
ORDERS_URL = "https://consumer-api.wolt.com/order-xp/web/v1/pages/orders"
TRACKING_URL = "https://restaurant-api.wolt.com/v2/order_details/purchase_tracking/{purchase_id}"

# Refresh the access token this long before it expires (it lives 1800 s).
TOKEN_REFRESH_MARGIN = 120

ORDER_STATUS_IN_PROGRESS = "IN_PROGRESS"  # orders page, telemetry.order_status_type
STATUS_READY = "ready"  # order_details.status while the courier is en route
TERMINAL_STATUSES = {"delivered", "rejected", "refunded"}
