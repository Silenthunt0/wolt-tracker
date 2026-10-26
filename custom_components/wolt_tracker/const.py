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

# Post-delivery rating ("How was it?"). Wolt keeps a delivered order
# "subscribed" until it is rated or the prompt is dismissed; the web app
# shows the two-step rating (food, then delivery) for delivered orders that
# are still subscribed and deletes the subscription afterwards.
SUBSCRIPTIONS_URL = "https://restaurant-api.wolt.com/v2/order_details/subscriptions"
SUBSCRIPTION_URL = "https://restaurant-api.wolt.com/v2/order_details/subscriptions/{purchase_id}"
REVIEW_TEMPLATE_URL = "https://restaurant-api.wolt.com/v1/reviews/purchases/{purchase_id}/template"
REVIEW_URL = "https://restaurant-api.wolt.com/v1/reviews/purchases/{purchase_id}"
RATING_SECTIONS = ("food", "delivery")  # step 1 and step 2 of the prompt
# People (and the services) rate 1..5; Wolt's API counts the smileys 0..4
# (verified from a captured web request: the top smiley is sent as 4).
RATING_MIN = 1
RATING_MAX = 5
RATING_API_OFFSET = 1  # api value = rating - offset
# Template "tags" that are UI buttons, not attributions: "comment" opens the
# comment box, "missing item" opens an item picker (sends an items list).
RATING_PSEUDO_TAGS = {"comment", "missing item"}

# Support chat ("Converse", Wolt's in-house chat backend behind the support
# widget). The Wolt access token is exchanged for a Converse token.
CONVERSE_BASE = "https://converse-api.wolt.com"
CONVERSE_APP_ID = "wolt-consumer"
CONVERSE_TOKEN_URL = f"{CONVERSE_BASE}/auth-api/v1/token"
CONVERSE_WIDGET_URL = f"{CONVERSE_BASE}/widget-api/v1/{CONVERSE_APP_ID}"
CONVERSE_TOKEN_REFRESH_MARGIN = 120  # s; the token lives 1800 s
SUPPORT_TRANSCRIPT_LIMIT = 10  # messages kept in the sensor attribute

# Fired once per new support message written by a Wolt agent.
EVENT_SUPPORT_MESSAGE = "wolt_tracker_support_message"

# Fired once per delivered order that is waiting for a rating.
EVENT_RATING_PENDING = "wolt_tracker_rating_pending"

SERVICE_REPLY = "reply"
SERVICE_MARK_READ = "mark_read"
SERVICE_RATE = "rate"
SERVICE_DISMISS_RATING = "dismiss_rating"
ATTR_MESSAGE = "message"
ATTR_CONVERSATION_ID = "conversation_id"
ATTR_PURCHASE_ID = "purchase_id"
ATTR_FOOD_RATING = "food_rating"
ATTR_FOOD_TAGS = "food_tags"
ATTR_FOOD_COMMENT = "food_comment"
ATTR_DELIVERY_RATING = "delivery_rating"
ATTR_DELIVERY_TAGS = "delivery_tags"
ATTR_DELIVERY_COMMENT = "delivery_comment"

# Refresh the access token this long before it expires (it lives 1800 s).
TOKEN_REFRESH_MARGIN = 120

ORDER_STATUS_IN_PROGRESS = "IN_PROGRESS"  # orders page, telemetry.order_status_type
STATUS_READY = "ready"  # order_details.status while the courier is en route
STATUS_DELIVERED = "delivered"
TERMINAL_STATUSES = {"delivered", "rejected", "refunded"}
SUPPORT_STATUS_OPENED = "opened"
SUPPORT_STATUS_RESOLVED = "resolved"
