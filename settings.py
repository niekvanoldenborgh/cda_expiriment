from os import environ

# If you installed dj_database_url, keep these 2 lines.
# If not, see the note at the bottom for the no-dj_database_url version.
import dj_database_url

DEBUG = True
SECRET_KEY = environ.get("OTREE_SECRET_KEY", "dev-secret-key-change-me")

AUTH_LEVEL = "DEMO"
ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = environ.get("OTREE_ADMIN_PASSWORD", "admin")

INSTALLED_APPS = ["otree"]

LANGUAGE_CODE = "en"

REAL_WORLD_CURRENCY_CODE = "EUR"
USE_POINTS = False

PARTICIPANT_FIELDS = []
SESSION_FIELDS = []

DATABASES = {
    "default": dj_database_url.config(default="sqlite:///db.sqlite3")
}

SESSION_CONFIG_DEFAULTS = dict(
    real_world_currency_per_point=1.00,
    participation_fee=0.00,
    doc="",
)

SESSION_CONFIGS = [
    # 1) Intro only (what you already had)
    dict(
        name="double_auction_intro",
        display_name="Double Auction – Introduction Only",
        app_sequence=["double_auction_intro"],
        num_demo_participants=1,

        experiment_country="EUR",
        no_transaction_costs=0,
        inflation_on=0,
        no_video_intro=0,
    ),

    # 2) Double Auction
    dict(
        name="double_auction_full",
        display_name="Double Auction – Main",
        app_sequence=["double_auction"],

        # IMPORTANT: must be divisible by (num_sellers + num_buyers).
        # Example: 4 sellers + 4 buyers = 8 participants.
        num_demo_participants=2,

        # Shared / intro params (keep if intro reads them)
        experiment_country="EUR",
        no_video_intro=1,

        # Main app params (double_auction)
        no_transaction_costs=0,      # 0 keep transaction-cost schedule, 1 remove
        inflation_on=0,
        inflation_rate=0.3,

        num_sellers=1,
        num_buyers=1,
        units_per_seller=5,
        units_per_buyer=20,

        time_per_round=90,
        time_per_round_currency=30,  # <-- if you prefer, set like 30
        drop_player_on=0,
        drop_player_time=30,

        # Sequence lengths (must sum to <= C.num_rounds if you use them that way)
        sequence_0=10,
        sequence_1=10,
        sequence_2=10,
        sequence_3=10,
        sequence_4=0,

        # Which sequences pay (your model logic uses these)
        paid_sequence_1=1,
        paid_sequence_2=2,

        # If your pages/models use this, include it
        currency_exchange_on=0,
    ),
]


