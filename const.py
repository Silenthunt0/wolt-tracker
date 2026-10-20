"""Constants for the Wolt order tracker."""

from datetime import timedelta

DOMAIN = "wolt_tracker"
STORAGE_KEY = f"{DOMAIN}.tokens"
STORAGE_VERSION = 1

CONF_REFRESH_TOKEN = "refresh_token"
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
AUTH_FAILED_INTERVAL = timedelta(minutes=10)

AUTH_URL = "https://authentication.wolt.com/v1/wauth2/access_token"
ORDERS_URL = "https://consumer-api.wolt.com/order-xp/web/v1/pages/orders"
TRACKING_URL = "https://restaurant-api.wolt.com/v2/order_details/purchase_tracking/{purchase_id}"

# Refresh the access token this long before it expires (it lives 1800 s).
TOKEN_REFRESH_MARGIN = 120

# Orders page: status types seen in telemetry.order_status_type
ORDER_STATUS_IN_PROGRESS = "IN_PROGRESS"
# order_details.status values seen in the web client
STATUS_READY = "ready"
TERMINAL_STATUSES = {"delivered", "rejected", "refunded"}

SERVICE_SET_REFRESH_TOKEN = "set_refresh_token"
NOTIFICATION_AUTH = f"{DOMAIN}_auth"
