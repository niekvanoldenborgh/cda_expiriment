from otree.api import (
    models, BaseConstants, BaseSubsession, BaseGroup, BasePlayer,
    Currency as c, currency_range, Page, WaitPage, ExtraModel, models
)

import json
from pprint import PrettyPrinter

from django.db import models as djmodels
from django.db.models import F, Q, Sum, ExpressionWrapper
from django.db.models.signals import post_save, pre_save
from django.template.loader import render_to_string
from django.utils.safestring import mark_safe


from django.db.models.query import QuerySet

from .exceptions import MarketException, NotEnoughFunds, NotEnoughItemsToSell, NoEndowment, NoItems
import time
import random




def dprint(object, stream=None, indent=1, width=80, depth=None):
    if getattr(object, "__metaclass__", None):
        if object.__metaclass__.__name__ == "ModelBase":
            object = object.__dict__
    elif isinstance(object, QuerySet):
        object = [i.__dict__ for i in object]
    printer = PrettyPrinter(stream=stream, indent=indent, width=width, depth=depth)
    printer.pprint(object)


class Constants(BaseConstants):
    # ---- REQUIRED BY OTREE (UPPERCASE) ----
    NAME_IN_URL = "double_auction_v2"
    PLAYERS_PER_GROUP = None
    NUM_ROUNDS = 200

    # ---- YOUR CONSTANTS (UPPERCASE CANONICAL) ----
    MULTIPLE_UNIT_TRADING = False
    PRICE_MAX_NUMBERS = 10
    PRICE_DIGITS = 2

    INITIAL_QUANTITY = 1
    SELLER_ENDOWMENT = 5
    BUYER_DOLLAR_ENDOWMENT = 100.00
    BUYER_BTC_ENDOWMENT = 100.00

    VARIABLE_PARAMS = (
        "num_sellers",
        "num_buyers",
        "units_per_seller",
        "units_per_buyer",
        "time_per_round",
        "time_per_round_currency",
        "drop_player_on",
        "drop_player_time",
        "inflation_on",
        "inflation_rate",
        "no_transaction_costs",
        "btc_transaction_factor",
        "dollar_freeze_enabled",
        "freeze_probability",
        "freeze_seed",
        "paid_sequence_1",
        "paid_sequence_2",
    )

    INSTRUCTIONS_TEMPLATE = "double_auction_v2/Instructions.html"

    # ---- LOWERCASE ALIASES (KEEP YOUR EXISTING CODE WORKING) ----
    name_in_url = NAME_IN_URL
    players_per_group = PLAYERS_PER_GROUP
    num_rounds = NUM_ROUNDS

    multiple_unit_trading = MULTIPLE_UNIT_TRADING
    price_max_numbers = PRICE_MAX_NUMBERS
    price_digits = PRICE_DIGITS

    initial_quantity = INITIAL_QUANTITY
    seller_endowment = SELLER_ENDOWMENT
    buyer_dollar_endowment = BUYER_DOLLAR_ENDOWMENT
    buyer_btc_endowment = BUYER_BTC_ENDOWMENT

    variable_params = VARIABLE_PARAMS
    instructions_template = INSTRUCTIONS_TEMPLATE


# keep C.xxx working everywhere else
C = Constants

SEQUENCE_COUNT = 4

# Sequence index treated as the training/practice block. Participants use it to
# learn the interface, so game mechanics that would confound learning (currently
# the Dollar freeze) are gated out for this sequence. See compute_dollar_frozen.
TRAINING_SEQUENCE_INDEX = 0


def _session_config_int(session, key):
    val = session.config.get(key)
    if val in (None, ""):
        raise ValueError(f"Missing required session config value: {key}")
    try:
        return int(val)
    except (TypeError, ValueError):
        raise ValueError(f"session.config['{key}'] must be an integer (got {val!r})")


def btc_transaction_factor_for_subsession(subsession):
    """
    Return the Currency B (bitcoin) transaction tax factor f for this subsession.

    f lives in (0, 1]. f == 1 means no tax. For a BTC trade at auction price
    P_hat the buyer pays P_hat / f and the seller receives P_hat, so the tax
    collected is P_hat / f - P_hat. Currency A is never taxed.

    Reads the registered Subsession field first (set from session.config by
    set_config); falls back to session.config and finally to 1.0 (no tax) so
    that sessions which never set the parameter behave exactly as before.
    """
    f = subsession.field_maybe_none("btc_transaction_factor")
    if f in (None, ""):
        f = subsession.session.config.get("btc_transaction_factor", 1)
    try:
        f = float(f)
    except (TypeError, ValueError):
        raise ValueError(
            f"btc_transaction_factor must be a number in (0, 1] (got {f!r})"
        )
    if not (0.0 < f <= 1.0):
        raise ValueError(
            f"btc_transaction_factor must be in (0, 1] (got {f})"
        )
    return f


def _btc_gross_price(price, f):
    """
    Buyer's tax-inclusive Currency B (bitcoin) price P_hat / f, rounded for DISPLAY.

    Shown in the dedicated "Incl. tax" table column next to the pre-tax auction
    price (which keeps its original meaning: the value the market matches on and
    that the SELLER receives). Returns None when the price is missing or f is
    unusable.

    Purely cosmetic: it NEVER feeds any affordability or tax computation; the tax
    logic in Bid/Ask.create_and_match and Contract.execute_trade is the single
    source of truth and is left exactly as-is.
    """
    if price in (None, ""):
        return None
    try:
        f = float(f)
    except (TypeError, ValueError):
        return None
    return round(float(price) / f, 2)


def btc_tax_active_for_subsession(subsession):
    """True when Currency B (bitcoin) trades are taxed (f < 1) for this subsession.

    Drives whether the display-only "Incl. tax" column is shown; when there is no
    tax the order book / trade history render exactly as before.
    """
    return btc_transaction_factor_for_subsession(subsession) < 1.0


def dollar_freeze_draw(freeze_seed, buyer_index, round_number):
    """
    Return omega's underlying uniform draw in [0, 1) for buyer position
    `buyer_index` in `round_number`, as a PURE FUNCTION of
    (freeze_seed, buyer_index, round_number).

    Why per-draw seeding (not a single global random.seed): if we seeded once
    and pulled from the shared stream inside per-player/per-page code, the draw a
    given buyer receives would depend on the order in which players/pages happen
    to consume the RNG, which is not stable across sessions or groups. Seeding a
    fresh RNG from (seed, buyer_index, round) makes each omega independent of
    consumption order, so the SAME buyer positions are frozen in the SAME periods
    across different sessions AND different groups for a fixed seed. Group/session
    ids are deliberately NOT part of the key, so the schedule is identical across
    groups and sessions.

    Note: the spec writes `random.Random((seed, i, t))`, but Python 3.11+ rejects
    tuple seeds, so we serialise the tuple into a stable string seed. String/bytes
    seeding is fully deterministic across processes/machines and is unaffected by
    PYTHONHASHSEED (unlike hash() of a tuple).
    """
    rng = random.Random(
        f"dollar_freeze|{int(freeze_seed)}|{int(buyer_index)}|{int(round_number)}"
    )
    return rng.random()


def is_dollar_frozen_for(freeze_seed, buyer_index, round_number, probability):
    """omega = 1 (frozen) iff the per-draw uniform is < probability."""
    return dollar_freeze_draw(freeze_seed, buyer_index, round_number) < probability


def dollar_frozen_exception():
    """Exception raised when a frozen buyer attempts to use dollars."""
    return MarketException(
        "Your Dollar (Currency A) account is frozen this period; you cannot use dollars.",
        {"warning": (
            "Your Dollar (Currency A) account is frozen this period. "
            "You cannot post or accept Dollar offers now. Your dollar balance "
            "is kept and will be usable again in a later period. Your Currency B "
            "(bitcoin) trading is unaffected."
        )},
    )


def dollar_freeze_params(subsession):
    """
    Return (enabled, probability, seed) for the dollar-freeze feature.

    Reads the registered Subsession fields first (set from session.config by
    set_config), falling back to session.config and finally to the feature-off
    defaults (disabled, probability 0, seed 0) so sessions that never set these
    behave exactly as before. probability is validated to lie in [0, 1].
    """
    cfg = subsession.session.config

    enabled = subsession.field_maybe_none("dollar_freeze_enabled")
    if enabled is None:
        enabled = cfg.get("dollar_freeze_enabled", False)
    enabled = bool(enabled)

    prob = subsession.field_maybe_none("freeze_probability")
    if prob in (None, ""):
        prob = cfg.get("freeze_probability", 0)
    try:
        prob = float(prob)
    except (TypeError, ValueError):
        raise ValueError(f"freeze_probability must be a number in [0, 1] (got {prob!r})")
    if not (0.0 <= prob <= 1.0):
        raise ValueError(f"freeze_probability must be in [0, 1] (got {prob})")

    seed = subsession.field_maybe_none("freeze_seed")
    if seed in (None, ""):
        seed = cfg.get("freeze_seed", 0)
    try:
        seed = int(seed)
    except (TypeError, ValueError):
        raise ValueError(f"freeze_seed must be an integer (got {seed!r})")

    return enabled, prob, seed


def block_length_for_session(session):
    value = _session_config_int(session, "block_length")
    if value <= 0:
        raise ValueError("session.config['block_length'] must be greater than 0")
    return value


def true_end_for_sequence(session, sequence_index):
    if sequence_index not in range(SEQUENCE_COUNT):
        raise ValueError(f"Invalid sequence index: {sequence_index}")
    value = _session_config_int(session, f"true_end_{sequence_index}")
    if value <= 0:
        raise ValueError(
            f"session.config['true_end_{sequence_index}'] must be greater than 0"
        )
    return value


def played_length_for_sequence(session, sequence_index):
    block_length = block_length_for_session(session)
    true_end = true_end_for_sequence(session, sequence_index)
    return ((true_end - 1) // block_length + 1) * block_length


def first_round_for_sequence(session, sequence_index):
    if sequence_index not in range(SEQUENCE_COUNT):
        raise ValueError(f"Invalid sequence index: {sequence_index}")
    return 1 + sum(
        played_length_for_sequence(session, i)
        for i in range(sequence_index)
    )


def visible_final_round_for_sequence(session, sequence_index):
    return (
        first_round_for_sequence(session, sequence_index)
        + played_length_for_sequence(session, sequence_index)
        - 1
    )


def true_end_global_round_for_sequence(session, sequence_index):
    return (
        first_round_for_sequence(session, sequence_index)
        + true_end_for_sequence(session, sequence_index)
        - 1
    )


def total_visible_rounds(session):
    return visible_final_round_for_sequence(session, SEQUENCE_COUNT - 1)


def current_sequence_index_for_round(session, round_number):
    if round_number < 1:
        raise ValueError(f"Invalid round number: {round_number}")
    if round_number > total_visible_rounds(session):
        return None
    for sequence_index in range(SEQUENCE_COUNT):
        if round_number <= visible_final_round_for_sequence(session, sequence_index):
            return sequence_index
    return None


def round_within_sequence_for_round(session, round_number):
    sequence_index = current_sequence_index_for_round(session, round_number)
    if sequence_index is None:
        return None
    return round_number - first_round_for_sequence(session, sequence_index) + 1


def validate_sequence_configuration(session):
    block_length_for_session(session)
    for sequence_index in range(SEQUENCE_COUNT):
        true_end_for_sequence(session, sequence_index)
    if total_visible_rounds(session) > C.NUM_ROUNDS:
        raise ValueError(
            f"Configured visible rounds ({total_visible_rounds(session)}) exceed "
            f"C.NUM_ROUNDS ({C.NUM_ROUNDS}) in double_auction_v2."
        )

def group_by_arrival_time_method(subsession, waiting_players):
    cfg = subsession.session.config
    group_size = cfg["num_sellers"] + cfg["num_buyers"]
    if len(waiting_players) >= group_size:
        return waiting_players

class Subsession(BaseSubsession):
    num_sellers = models.IntegerField()
    num_buyers = models.IntegerField()
    units_per_seller = models.IntegerField()
    units_per_buyer = models.IntegerField()
    time_per_round = models.IntegerField()
    time_per_round_currency = models.IntegerField()
    drop_player_on = models.IntegerField()
    drop_player_time = models.IntegerField()
    inflation_on = models.IntegerField()
    inflation_rate = models.FloatField()
    no_transaction_costs = models.IntegerField()
    # Currency B (bitcoin) transaction tax factor f, in (0, 1]. f = 1 => no tax.
    btc_transaction_factor = models.FloatField()
    # Dollar (Currency A) freeze feature. When enabled, each buyer's dollar
    # account may be frozen (unusable) for a period with probability
    # freeze_probability, drawn reproducibly from freeze_seed. See the freeze
    # helpers below. Disabled by default => no behavioral change.
    dollar_freeze_enabled = models.BooleanField()
    freeze_probability = models.FloatField()
    freeze_seed = models.IntegerField()
    sequence_0 = models.IntegerField()
    sequence_1 = models.IntegerField()
    sequence_2 = models.IntegerField()
    sequence_3 = models.IntegerField()
    sequence_4 = models.IntegerField()
    paid_sequence_1 = models.IntegerField()
    paid_sequence_2 = models.IntegerField()

    def set_config(self):
        for k in C.variable_params:
            setattr(self, k, self.session.config.get(k))
        for g in self.get_groups():
            g.set_role()




class Group(BaseGroup):
    active = models.BooleanField(initial=True)
    After_CE = models.BooleanField(initial=False)
    round_within_sequence = models.IntegerField(initial=1)
    sequence_number = models.IntegerField(initial=1)
    total_round = models.IntegerField()
    drawn_number = models.IntegerField()

    # -----------------------
    # Helpers: session.config
    # -----------------------
    def cfg(self):
        # shorthand
        return self.subsession.session.config

    def cfg_int(self, key: str, default: int = 0) -> int:
        """
        Robust int reader for session.config keys.
        Accepts None, '', '10', 10.0, etc.
        """
        val = self.cfg().get(key, default)
        if val in (None, ""):
            return int(default)
        try:
            return int(val)
        except (TypeError, ValueError):
            raise ValueError(
                f"session.config['{key}'] must be an integer (got {val!r}). "
                "If you intended a list, adjust code to use len(list)."
            )

    def sequence_count(self):
        return SEQUENCE_COUNT

    def block_length(self):
        return block_length_for_session(self.subsession.session)

    def true_end_for_sequence(self, sequence_index):
        return true_end_for_sequence(self.subsession.session, sequence_index)

    def played_length_for_sequence(self, sequence_index):
        return played_length_for_sequence(self.subsession.session, sequence_index)

    def first_round_for_sequence(self, sequence_index):
        return first_round_for_sequence(self.subsession.session, sequence_index)

    def visible_final_round_for_sequence(self, sequence_index):
        return visible_final_round_for_sequence(self.subsession.session, sequence_index)

    def true_end_global_round_for_sequence(self, sequence_index):
        return true_end_global_round_for_sequence(self.subsession.session, sequence_index)

    def total_visible_rounds(self):
        return total_visible_rounds(self.subsession.session)

    def is_active_round(self):
        return self.round_number <= self.total_visible_rounds()

    def current_sequence_index(self):
        return current_sequence_index_for_round(self.subsession.session, self.round_number)

    def current_round_within_sequence(self):
        return round_within_sequence_for_round(self.subsession.session, self.round_number)

    def current_sequence_true_end(self):
        return self.true_end_for_sequence(self.sequence_number)

    def current_sequence_played_length(self):
        return self.played_length_for_sequence(self.sequence_number)

    def current_sequence_true_end_global_round(self):
        return self.true_end_global_round_for_sequence(self.sequence_number)

    def current_sequence_first_round(self):
        return self.first_round_for_sequence(self.sequence_number)

    def current_sequence_visible_final_round(self):
        return self.visible_final_round_for_sequence(self.sequence_number)

    def current_block_number(self):
        if not self.is_active_round():
            return None
        return ((self.round_within_sequence - 1) // self.block_length()) + 1

    def current_block_start_round(self):
        if not self.is_active_round():
            return None
        return (self.current_block_number() - 1) * self.block_length() + 1

    def current_block_end_round(self):
        if not self.is_active_round():
            return None
        return self.current_block_number() * self.block_length()

    def is_end_of_block(self):
        return self.is_active_round() and self.round_within_sequence % self.block_length() == 0

    def current_block_contains_true_end(self):
        if not self.is_active_round():
            return False
        return (
            self.current_block_start_round()
            <= self.current_sequence_true_end()
            <= self.current_block_end_round()
        )

    def is_final_visible_block_of_sequence(self):
        if not self.is_active_round():
            return False
        return self.current_block_end_round() == self.current_sequence_played_length()

    def discarded_rounds_in_current_block(self):
        if not (self.is_end_of_block() and self.current_block_contains_true_end()):
            return 0
        return self.current_block_end_round() - self.current_sequence_true_end()

    # -----------------------
    # Existing functions
    # -----------------------
    def btc_tax_active(self):
        """True when Currency B (bitcoin) is taxed (f < 1); drives the display-only
        'Incl. tax' column. No tax => column hidden, layout unchanged."""
        return btc_tax_active_for_subsession(self.subsession)

    def get_channel_group_name(self):
        return f"double_auction_group_{self.pk}"

    def get_players_by_role(self, role):
        return [p for p in self.get_players() if p.role() == role]

    def get_buyers(self):
        return self.get_players_by_role("buyer")

    def get_sellers(self):
        return self.get_players_by_role("seller")


    def get_contracts(self, house):
        return Contract.filter(group=self, BTC_Statement=house)

    def total_btc_tax_collected(self):
        """
        Sum of Currency B (bitcoin) transaction tax collected by THIS group's
        market in THIS round. Dollar trades record tax_paid_btc == 0, so summing
        over all contracts of the group yields only the BTC tax.
        """
        total = 0.0
        for contract in Contract.filter(group=self):
            # ExtraModels have no field_maybe_none; tax_paid_btc has initial=0.0,
            # but guard against NULL on rows created before this column existed.
            try:
                tax = contract.tax_paid_btc
            except Exception:
                tax = None
            total += float(tax or 0.0)
        return total


    def get_bids(self, house):
        bids = Bid.filter(group=self, active=True, BTC_Statement=house)
        bids.sort(key=lambda b: (-(b.price or float("-inf")), b.created_at or 0))
        return bids
    
    def get_asks(self, house):
        asks = Ask.filter(group=self, active=True, BTC_Statement=house)
        asks.sort(key=lambda a: (a.price if a.price is not None else float("inf"),
                                 a.created_at if a.created_at is not None else 0))
        return asks

    def get_spread_html(self, house):
        tpl = (
            "double_auction_v2/includes/spread_to_render_btc.html"
            if house is True
            else "double_auction_v2/includes/spread_to_render.html"
        )
        return mark_safe(render_to_string(tpl, {"group": self}))

    def no_buyers_left(self, house) -> bool:
        if house is False:
            return not any([p.active_dollar for p in self.get_buyers()])
        return not any([p.active_btc for p in self.get_buyers()])

    def no_sellers_left(self, house) -> bool:
        if house is False:
            return not any([p.active_dollar for p in self.get_sellers()])
        return not any([p.active_btc for p in self.get_sellers()])

    def is_market_closed(self, house) -> bool:
        return self.no_buyers_left(house) or self.no_sellers_left(house)

    def best_ask(self, house = False):
        asks = self.get_asks(house)  # list
        if not asks:
            return None
        # "best ask" = lowest price (and earliest created_at as tie-break)
        asks_sorted = sorted(
            asks,
            key=lambda a: (
                a.price if a.price is not None else float("inf"),
                a.created_at if getattr(a, "created_at", None) is not None else 0,
            ),
        )
        return asks_sorted[0]

        
    @property
    def best_ask_dollar(self):
        ask = self.best_ask(False)
        return ask if ask is not None else False

    @property
    def best_ask_btc(self):
        ask = self.best_ask(True)
        return ask if ask is not None else False
    
    def best_bid(self, house=False):
        bids = self.get_bids(house)
        if not bids:
            return None

        bids.sort(
            key=lambda b: (
                -(b.price if b.price is not None else float("-inf")),
                b.created_at if getattr(b, "created_at", None) is not None else 0,
            )
        )
        return bids[0]


    @property
    def best_bid_dollar(self):
        bid = self.best_bid(False)
        return bid if bid is not None else False
        
  

    @property
    def best_bid_btc(self):
        bid = self.best_bid(True)
        return bid if bid is not None else False

    def presence_check(self):
        msg = {"market_over": False}
        house_dollar_inactive = (self.no_buyers_left(False) or self.no_sellers_left(False))
        house_btc_inactive = (self.no_buyers_left(True) or self.no_sellers_left(True))
        if house_dollar_inactive and house_btc_inactive:
            self.active = False
            msg = {"market_over": True, "over_message": "No trading partners remaining"}
        return msg

    def reactive(self):
        self.active = True
        for p in self.get_players():
            p.active_dollar = p.is_active(False)
            p.active_btc = p.is_active(True)
        house_dollar_inactive = (self.no_buyers_left(False) or self.no_sellers_left(False))
        house_btc_inactive = (self.no_buyers_left(True) or self.no_sellers_left(True))
        if house_dollar_inactive and house_btc_inactive:
            self.active = False

    def clear_ask_bid(self):
        for b in Bid.filter(group=self, active=True):
            b.active = False


        for a in Ask.filter(group=self, active=True):
            a.active = False



    # -----------------------
    # SEQUENCE LOGIC (rewritten)
    # -----------------------
    def final_round_in_sequence(self):
        return [
            self.visible_final_round_for_sequence(sequence_index)
            for sequence_index in range(self.sequence_count())
        ]

    def true_end_rounds_in_sequence(self):
        return [
            self.true_end_global_round_for_sequence(sequence_index)
            for sequence_index in range(self.sequence_count())
        ]

    def first_round_in_sequence(self):
        return [
            self.first_round_for_sequence(sequence_index)
            for sequence_index in range(self.sequence_count())
        ]

    def set_sequence(self):
        sequence_index = self.current_sequence_index()
        if sequence_index is None:
            self.sequence_number = self.sequence_count()
            self.round_within_sequence = 0
            return
        self.sequence_number = sequence_index
        self.round_within_sequence = self.current_round_within_sequence()

    def set_total_round(self):
        self.total_round = self.total_visible_rounds()

    def is_last_sequence(self):
        self.set_total_round()
        return 1 if self.total_round == self.round_number else 0

    # -----------------------
    # Roles (unchanged logic)
    # -----------------------
    def get_active_groupid(self):
        return [p.id_in_group for p in self.get_players() if p.disconnection_flag == 0]

    def set_role(self):
        active_groupid = self.get_active_groupid()
        if len(active_groupid) % 2 == 1:
            print("Error: odd number of active players.")
        buyer_group = active_groupid[0: int(len(active_groupid) / 2)]

        for p in self.get_players():
            if self.round_number in self.first_round_in_sequence():
                p.role_code = "buyer" if p.id_in_group in buyer_group else "seller"
            else:
                prev = p.in_round(p.round_number - 1).role()
                p.role_code = "buyer" if prev == "seller" else "seller"



class Player(BasePlayer):
    active_dollar = models.BooleanField(initial=True)
    active_btc = models.BooleanField(initial=True)
    endowment = models.FloatField(initial=0)

    # omega for this buyer this round: True => Dollar (Currency A) account frozen
    # (unusable) this period. Always False for sellers and whenever the feature is
    # disabled. The dollar BALANCE is never zeroed; only its usability is gated.
    dollar_frozen = models.BooleanField(initial=False)

    units_dollar = models.IntegerField(initial=0)
    units_btc = models.IntegerField(initial=0)

    thisround_dollar = models.FloatField(initial=0.0)
    thisround_btc = models.FloatField(initial=0.0)

    thisround_profit = models.FloatField(initial=0)
    thissequence_profit = models.FloatField(initial=0)

    thisround_euro = models.CurrencyField(initial=0)
    thissequence_euro = models.CurrencyField(initial=0)

    # 0 normal, 1 disconnected/no response, 2 dropped to balance
    disconnection_flag = models.IntegerField(initial=0)
    role_code = models.StringField()
    no_action1 = models.BooleanField(initial=False)

    exit_fee = models.CurrencyField(initial=0)
    drop_sequence = models.IntegerField()
    payment_message = models.StringField(initial="")

    allocation_unit_dollar = models.IntegerField(min=0, max=C.seller_endowment)
    allocation_unit_btc = models.IntegerField(min=0, blank=True)

    allocation_dollar = models.FloatField(min=0, max=C.buyer_dollar_endowment, initial=C.buyer_dollar_endowment)
    allocation_btc = models.FloatField(min=0, max=C.buyer_btc_endowment, initial=C.buyer_btc_endowment)

    allocation_dollar_save = models.FloatField(min=0, blank=True, initial=0)
    allocation_btc_save = models.FloatField(min=0, blank=True, initial=0)
    
    @property
    def action_name(self):
        # example logic â€” adapt to your app
        if self.role() == "buyer":
            return "bid"
        return "ask"
        
    @property
    def role_str(self):
        return "buyer" if self.role_code == "buyer" else "seller"
        
    def is_display(self):
        player_active = self.active_btc or self.active_dollar
        return player_active and self.group.active

    def Currency_symbol(self):
        if self.session.config["experiment_country"].upper() == "CAD":
            return "$"
        elif self.session.config["experiment_country"].upper() == "EUR":
            return "€"

    def Currency_name(self):
        if self.session.config["experiment_country"].upper() == "CAD":
            return "Canadian dollar"
        elif self.session.config["experiment_country"].upper() == "EUR":
            return "Euro"

    def Currency_contact_info(self):
        if self.session.config["experiment_country"].upper() == "CAD":
            return "email address"
        elif self.session.config["experiment_country"].upper() == "EUR":
            return "IBAN"

    def inflation_info(self):
        return "" if self.session.config["inflation_on"] == 1 else "hidden"

    def no_transaction_hidden(self):
        return "hidden" if self.session.config["no_transaction_costs"] == 1 else ""

    def no_transaction_show(self):
        return "" if self.session.config["no_transaction_costs"] == 1 else "hidden"

    def count_online_players(self):
        return sum(1 for p in self.group.get_players() if p.disconnection_flag == 0)

    def current_sequence_index(self):
        return current_sequence_index_for_round(self.session, self.round_number)

    def round_within_current_sequence(self):
        return round_within_sequence_for_round(self.session, self.round_number)

    def round_counts_for_payoff(self):
        sequence_index = self.current_sequence_index()
        if sequence_index is None:
            return False
        return self.round_within_current_sequence() <= true_end_for_sequence(
            self.session, sequence_index
        )

    def counted_sequence_euro(self, sequence_index=None):
        if sequence_index is None:
            sequence_index = self.current_sequence_index()
        if sequence_index is None:
            return c(0)

        first_round = first_round_for_sequence(self.session, sequence_index)
        counted_last_round = min(
            self.round_number,
            true_end_global_round_for_sequence(self.session, sequence_index),
        )
        if counted_last_round < first_round:
            return c(0)

        total = c(0)
        for round_number in range(first_round, counted_last_round + 1):
            total += self.in_round(round_number).thisround_euro
        return total

    def counted_sequence_profit(self, sequence_index=None):
        if sequence_index is None:
            sequence_index = self.current_sequence_index()
        if sequence_index is None:
            return 0.0

        first_round = first_round_for_sequence(self.session, sequence_index)
        counted_last_round = min(
            self.round_number,
            true_end_global_round_for_sequence(self.session, sequence_index),
        )
        if counted_last_round < first_round:
            return 0.0

        total = 0.0
        for round_number in range(first_round, counted_last_round + 1):
            total += self.in_round(round_number).thisround_profit
        return total

    def drop_one(self):
        self.disconnection_flag = 1
        for i in range(self.round_number, C.num_rounds):
            self.in_round(i).subsession.num_sellers -= 1
            self.in_round(i).subsession.num_buyers -= 1

        def _drop_partner(target_role):
            for p in self.group.get_players():
                if p.role() == target_role:
                    p.disconnection_flag = 2
                    p.drop_sequence = p.group.sequence_number
                    if p.group.sequence_number <= 1:
                        p.drop_sequence = 1
                        for i in range(p.round_number):
                            p.in_round(i + 1).payoff = 0
                        p.payoff = c(15)
                    elif p.group.sequence_number == 2:
                        p.drop_sequence = 2
                        for i in range(p.round_number):
                            round_player = p.in_round(i + 1)
                            if (
                                round_player.group.sequence_number in [1]
                                and round_player.round_counts_for_payoff()
                            ):
                                round_player.payoff = round_player.thisround_euro
                            else:
                                round_player.payoff = 0
                        p.payoff = c(12)
                    else:
                        p.drop_sequence = 3
                        for i in range(p.round_number):
                            round_player = p.in_round(i + 1)
                            if (
                                round_player.group.sequence_number in [1, 2]
                                and round_player.round_counts_for_payoff()
                            ):
                                round_player.payoff = round_player.thisround_euro
                            else:
                                round_player.payoff = 0
                    break

        if self.role() == "buyer":
            _drop_partner("seller")
        else:
            _drop_partner("buyer")

        self.save()

    def role(self):
        return "buyer" if self.role_code == "buyer" else "seller"

    def set_units(self, house):
        slots = Slot.filter(owner=self, BTC=house)

        total_qty = 0
        for sl in slots:
            for it in Item.filter(slot=sl):
                total_qty += it.quantity

        if house is False:
            self.units_dollar = total_qty
        else:
            self.units_btc = total_qty

        return total_qty

    def set_this_round_payoff(self):
        self.thisround_profit = 0
        self.thisround_euro = c(0)

        contracts_dollar = self.get_contracts_queryset(False)
        if contracts_dollar:
            self.thisround_profit += sum([p.profit for p in contracts_dollar])

        contracts_btc = self.get_contracts_queryset(True)
        if contracts_btc:
            self.thisround_profit += sum([p.profit for p in contracts_btc])

        self.thisround_euro = c(round((self.thisround_profit) ** 0.5 / 10, 2))

    def set_sequence_payoff(self):
        # Keep the running display provisional until the block-ending reveal.
        self.thissequence_profit += self.thisround_profit
        self.thissequence_euro += self.thisround_euro

    def set_payoff(self):
        self.payoff = 0
        if self.disconnection_flag == 2:
            return

        if (
            self.current_sequence_index() in
            [self.subsession.paid_sequence_1, self.subsession.paid_sequence_2]
            and self.round_counts_for_payoff()
        ):
            self.payoff = self.thisround_euro

    def set_payment_message(self):
        if self.disconnection_flag == 2:
            if self.drop_sequence == 1:
                self.payment_message = f"Since you are dropped out before Sequence 2, you will be compensated with {self.Currency_symbol()}{c(15)}."
            elif self.drop_sequence == 2:
                sequence_payment_1 = self.counted_sequence_euro(1)
                self.payment_message = (
                    f"Since you are dropped out in Sequence 2, you will earn euro you made in Sequence 1: "
                    f"{sequence_payment_1} plus a compensation: {self.Currency_symbol()}{c(12)}."
                )
            else:
                sequence_payment_1 = self.counted_sequence_euro(1)
                sequence_payment_2 = self.counted_sequence_euro(2)
                self.payment_message = (
                    f"Since you are dropped out after Sequence 2, you will earn euro you made in Sequence 1: "
                    f"{sequence_payment_1} and Sequence 2: {sequence_payment_2}."
                )
        else:
            seq1 = self.counted_sequence_euro(self.subsession.paid_sequence_1)
            seq2 = self.counted_sequence_euro(self.subsession.paid_sequence_2)
            self.payment_message = (
                f"Sequences {self.subsession.paid_sequence_1} and Sequence {self.subsession.paid_sequence_2} got selected for your earnings. "
                f"You made {seq1} in Sequences {self.subsession.paid_sequence_1}, and "
                f"{seq2} in Sequences {self.subsession.paid_sequence_2}."
            )

    def inherit_sequence_payoff(self):
        if self.round_number != 1:
            self.thissequence_profit = self.in_round(self.round_number - 1).thissequence_profit
            self.thissequence_euro = self.in_round(self.round_number - 1).thissequence_euro

    def clear_sequence_payoff(self):
        if self.round_number in self.group.first_round_in_sequence():
            self.thissequence_profit = 0
            self.thissequence_euro = 0

    def btc_tax_active(self):
        """True when Currency B (bitcoin) is taxed (f < 1); drives the display-only
        'Incl. tax' column. No tax => column hidden, layout unchanged."""
        return btc_tax_active_for_subsession(self.subsession)

    def is_dollar_frozen(self):
        """omega for this buyer this round (True => Dollar account frozen).

        Robust against a NULL field; sellers/disabled-feature are always False.
        """
        return bool(self.field_maybe_none("dollar_frozen"))

    def compute_dollar_frozen(self):
        """
        Set self.dollar_frozen for this round, deterministically.

        Only buyers can be frozen, and only when the feature is enabled. The draw
        is a pure function of (freeze_seed, id_in_group, round_number) so it is
        reproducible across sessions and groups (see dollar_freeze_draw). Sellers
        and the feature-off case are always False.

        The training sequence (sequence index 0) is never frozen: it exists so
        participants can practice the interface, so the freeze mechanic is gated
        out there regardless of seed/probability. Note this consumes no draw, so
        the (seed, id_in_group, round_number) schedule for the paid sequences is
        unchanged by this gate.
        """
        enabled, prob, seed = dollar_freeze_params(self.subsession)
        if not enabled or self.role() != "buyer":
            self.dollar_frozen = False
            return
        # Skip the freeze during the training/practice sequence (index 0).
        if self.current_sequence_index() == TRAINING_SEQUENCE_INDEX:
            self.dollar_frozen = False
            return
        self.dollar_frozen = is_dollar_frozen_for(
            seed, self.id_in_group, self.round_number, prob
        )

    def is_active(self, house):
        if house is False:
            if self.role() == "buyer":
                # A frozen buyer has no USABLE dollars this period (balance is
                # preserved, just unusable). Currency B is unaffected.
                if self.is_dollar_frozen():
                    return False
                return self.allocation_dollar > 0 and self.has_free_slots(house)
            return bool(self.get_full_slots(house))   # <- FIX
        else:
            if self.role() == "buyer":
                return self.allocation_btc > 0 and self.has_free_slots(house)
            return bool(self.get_full_slots(house))   # <- FIX


    def get_items(self):
        items = []
        for sl in Slot.filter(owner=self):
            items += Item.filter(slot=sl)
        return items


    def get_slots(self, house):
        # Slot is almost certainly an ExtraModel, so use Slot.filter
        return Slot.filter(owner=self, BTC=house)

    def has_free_slots(self, house):
        # free slot = a slot with no Item attached
        for sl in Slot.filter(owner=self, BTC=house):
            if not Item.filter(slot=sl):
                return True
        return False

    def get_free_slot(self, house):
        # choose the highest-value free slot (like your old order_by("-value").first())
        free = []
        for sl in Slot.filter(owner=self, BTC=house):
            if not Item.filter(slot=sl):
                free.append(sl)
        if not free:
            return None
        free.sort(key=lambda s: (s.value if s.value is not None else float("-inf")), reverse=True)
        return free[0]


    def get_full_slots(self, house):
        # all slots owned by this player in this house
        slots = Slot.filter(owner=self, BTC=house)

        full = []
        for sl in slots:
            # if any Item exists for this slot, it's "full"
            if Item.filter(slot=sl):
                full.append(sl)
        return full

    def presence_check(self):
        # original code ended by forcing False to let inactive buyers watch; keep that behavior:
        return {"market_over": False}

    def get_repo_context(self, house):
        slots = Slot.filter(owner=self, BTC=house)

        # attach a computed .quantity attribute for templates
        for sl in slots:
            items = Item.filter(slot=sl)
            sl.quantity = items[0].quantity if items else 0

        if self.role() == "seller":
            slots.sort(key=lambda s: (s.cost if s.cost is not None else float("inf")))
        else:
            slots.sort(key=lambda s: (s.value if s.value is not None else float("-inf")), reverse=True)

        return slots

    def get_repo_html(self, house):
        tpl = "double_auction_v2/includes/repo_to_render.html" if house is False else "double_auction_v2/includes/repo_to_render_btc.html"
        key = "repository" if house is False else "repository_btc"
        return mark_safe(render_to_string(tpl, {key: self.get_repo_context(house)}))

    def get_asks_html(self, house):
        asks = self.group.get_asks(house)
        return mark_safe(render_to_string("double_auction_v2/includes/asks_to_render.html", {"asks": asks, "player": self}))

    def get_bids_html(self, house):
        bids = self.group.get_bids(house)
        return mark_safe(render_to_string("double_auction_v2/includes/bids_to_render.html", {"bids": bids, "player": self}))

    def get_contracts_queryset(self, house):
        contracts = self.get_contracts(house)

        for c in contracts:
            # get linked bid / ask
            b = getattr(c, "bid", None)
            a = getattr(c, "ask", None)

            # price
            c.price = (
                getattr(c, "price", None)
                or getattr(b, "price", None)
                or getattr(a, "price", None)
                or 0
            )

            # quantity
            c.quantity = (
                getattr(c, "quantity", None)
                or getattr(b, "quantity", None)
                or getattr(a, "quantity", None)
                or 0
            )

            # cost/value + profit
            if self.role() == "seller":
                c.cost_value = getattr(c, "cost", 0) or 0
                c.profit = c.cost_value
            else:
                c.cost_value = getattr(c, "value", 0) or 0
                c.profit = c.cost_value

        return contracts


    def get_contracts_html(self, house):
        tpl = "double_auction_v2/includes/contracts_to_render.html" if house is False else "double_auction_v2/includes/contracts_to_render_btc.html"
        key = "contracts" if house is False else "contracts_btc"
        return mark_safe(render_to_string(tpl, {key: self.get_contracts_queryset(house), "player": self}))

    def get_form_context(self, house):
        house_dollar_inactive = (self.group.no_buyers_left(False) or self.group.no_sellers_left(False))
        house_btc_inactive = (self.group.no_buyers_left(True) or self.group.no_sellers_left(True))

        # Helper: "exists" for either list or None
        def any_rows(x):
            return bool(x)  # True if list non-empty

        if self.role() == "buyer":
            no_statements = not any_rows(self.get_bids(house))

            # Guard against None fields (oTree 6 null-field rule)
            alloc_dollar = self.field_maybe_none("allocation_dollar") or 0
            alloc_btc = self.field_maybe_none("allocation_btc") or 0

            # A frozen buyer's Dollar interface is disabled this period (BTC is
            # untouched). This also drives the live-update form_state, so the
            # disable persists across live refreshes.
            no_slots_or_funds_dollar = (alloc_dollar <= 0) or (not self.has_free_slots(False)) or house_dollar_inactive or self.is_dollar_frozen()
            no_slots_or_funds_btc = (alloc_btc <= 0) or (not self.has_free_slots(True)) or house_btc_inactive

        else:
            no_slots_or_funds_dollar = (not any_rows(self.get_full_slots(False))) or house_dollar_inactive
            no_slots_or_funds_btc = (not any_rows(self.get_full_slots(True))) or house_btc_inactive
            no_statements = not any_rows(self.get_asks(house))

        return {
            "no_slots_or_funds_dollar": no_slots_or_funds_dollar,
            "no_slots_or_funds_btc": no_slots_or_funds_btc,
            "no_statements": no_statements,
        }

    def get_form_html(self, house):
        context = self.get_form_context(house)
        context["player"] = self
        tpl = "double_auction_v2/includes/form_to_render.html" if house is False else "double_auction_v2/includes/form_to_render_btc.html"
        return mark_safe(render_to_string(tpl, context))


    def profit_block_html(self, house):
        self.set_units(house)
        tpl = "double_auction_v2/includes/profit_to_render.html" if house is False else "double_auction_v2/includes/profit_to_render_btc.html"
        return mark_safe(render_to_string(tpl, {"player": self}))


    def general_info_block_html(self):
        return mark_safe(render_to_string(
            "double_auction_v2/includes/general_info_to_render.html",
            {"player": self, "group": self.group},
        ))


    def get_contracts(self, house):
        contracts = Contract.filter(group=self.group, round_contract=self.round_number)
        my_contracts = []
        for c in contracts:
            b = getattr(c, "bid", None)
            a = getattr(c, "ask", None)

            # defensive: links can be missing if data inconsistent
            bid_ok = (b is not None and getattr(b, "player", None) == self and getattr(b, "BTC_Statement", None) == house)
            ask_ok = (a is not None and getattr(a, "player", None) == self and getattr(a, "BTC_Statement", None) == house)

            if bid_ok or ask_ok:
                my_contracts.append(c)

        return my_contracts

    def get_bids(self, house):
        bids = Bid.filter(player=self, active=True, BTC_Statement=house)
        bids.sort(key=lambda b: (-(b.price or float("-inf")), b.created_at or 0))
        return bids


    def get_asks(self, house):
        asks = Ask.filter(player=self, active=True, BTC_Statement=house)
        asks.sort(key=lambda a: (a.price if a.price is not None else float("inf"),
                                 a.created_at if a.created_at is not None else 0))
        return asks
        

    def action_name_reverse(self):
        if self.role() == 'buyer':
            return 'ask'
        return 'bid'

        
    def get_last_statement(self):
        # dollar market (BTC_Statement=False)
        
        
        
        if self.role() == "seller":
            stmts = Ask.filter(player=self, active=True, BTC_Statement=False)
        else:
            stmts = Bid.filter(player=self, active=True, BTC_Statement=False)

        if not stmts:
            return None
    
        
        stmts.sort(key=lambda s: (getattr(s, "created_at", 0) or 0), reverse=True)
        
        
        return stmts[0]


    def get_last_statement_btc(self):
        # btc market (BTC_Statement=True)
        if self.role() == "seller":
            stmts = Ask.filter(player=self, active=True, BTC_Statement=True)
        else:
            stmts = Bid.filter(player=self, active=True, BTC_Statement=True)

        if not stmts:
            return None
        
        

        stmts.sort(key=lambda s: (getattr(s, "created_at", 0) or 0), reverse=True)
        return stmts[0]



    def item_to_sell(self, house):
        full_slots = self.get_full_slots(house)  # should be a list

        # sort by cost (ascending); missing cost goes last
        full_slots.sort(key=lambda s: (s.cost if s.cost is not None else float("inf")))

        if not full_slots:
            return None

        first_slot = full_slots[0]
        items = Item.filter(slot=first_slot)
        return items[0] if items else None


    def get_personal_channel_name(self):
        return f"{self.role()}_{self.id}"





def _now():
    return time.time()


class Ask(ExtraModel):
    group = models.Link(Group)
    player = models.Link(Player)
    BTC_Statement = models.BooleanField()
    price = models.FloatField()
    quantity = models.IntegerField()
    quantity_initial = models.IntegerField()
    round_statement = models.IntegerField()
    active = models.BooleanField(initial=True)
    created_at = models.FloatField(initial=_now)

    @property
    def price_incl_tax(self):
        """Display-only tax-inclusive price P_hat/f for this ask (see
        _btc_gross_price). None for Currency A (dollar) statements."""
        if not self.BTC_Statement:
            return None
        return _btc_gross_price(
            self.price, btc_transaction_factor_for_subsession(self.group.subsession)
        )

    @classmethod
    def active_qs(cls, group, house):
        # returns a LIST of Ask objects
        return cls.filter(active=True, group=group, BTC_Statement=house)

    @classmethod
    def create_and_match(cls, *, player, BTC_Statement, price, quantity, round_statement):

        # ---- pre-check: seller has enough items in this house
        slots = Slot.filter(owner=player, BTC=BTC_Statement)
       
        items_count = 0
        for sl in slots:
            for it in Item.filter(slot=sl):
                items_count += int(it.quantity or 0)

        if items_count < int(quantity):
            raise NotEnoughItemsToSell(player, BTC_Statement, items_count)

        group = player.group
        price = float(price)
        quantity = int(quantity)
        round_statement = int(round_statement)

        # ---- deactivate previous asks from this player/house/round
        old_asks = cls.filter(
            active=True,
            group=group,
            player=player,
            BTC_Statement=BTC_Statement,
            round_statement=round_statement,
        )
        for a in old_asks:
            a.active = False
        
        

        # ---- create new ask
        ask = cls.create(
            group=group,
            player=player,
            BTC_Statement=BTC_Statement,
            price=price,
            quantity=quantity,
            quantity_initial=quantity,
            round_statement=round_statement,
            active=True,
            created_at=_now(),
        )

        # ---- match loop
        while ask.active and ask.quantity > 0:

            bids = Bid.filter(
                active=True,
                group=group,
                BTC_Statement=BTC_Statement,
                round_statement=round_statement,
            )

            # keep only bids that meet price
            bids = [b for b in bids if float(b.price) >= float(ask.price)]
            if not bids:
                break

            # pick best bid: highest price, then earliest created_at
            bids.sort(key=lambda b: (-float(b.price), float(b.created_at)))
            bid = bids[0]

            # funds check on buyer
            if bid.BTC_Statement is False:
                # Currency A: a frozen buyer has zero usable dollars (defense in
                # depth; frozen buyers never hold active Dollar bids to match).
                if bid.player.is_dollar_frozen():
                    raise dollar_frozen_exception()
                # Currency A: untaxed, check against P_hat.
                if bid.player.allocation_dollar < float(bid.price):
                    raise NotEnoughFunds(bid.player, bid.BTC_Statement, bid.player.allocation_dollar)
            else:
                # Currency B: buyer pays P_hat / f, so check against P_hat / f.
                f = btc_transaction_factor_for_subsession(bid.player.subsession)
                if bid.player.allocation_btc < float(bid.price) / f:
                    raise NotEnoughFunds(bid.player, bid.BTC_Statement, bid.player.allocation_btc)

            item = player.item_to_sell(BTC_Statement)
            if not item:
                # seller ran out unexpectedly; stop
                break

            Contract.execute_trade(
                group=group,
                item=item,
                bid=bid,
                ask=ask,
                price=min(float(bid.price), float(ask.price)),
            )


        return ask



class Bid(ExtraModel):
    group = models.Link(Group)
    player = models.Link(Player)
    BTC_Statement = models.BooleanField()
    price = models.FloatField()
    quantity = models.IntegerField()
    quantity_initial = models.IntegerField()
    round_statement = models.IntegerField()
    active = models.BooleanField(initial=True)
    created_at = models.FloatField(initial=_now)

    @property
    def price_incl_tax(self):
        """Display-only tax-inclusive price P_hat/f for this bid (see
        _btc_gross_price). None for Currency A (dollar) statements."""
        if not self.BTC_Statement:
            return None
        return _btc_gross_price(
            self.price, btc_transaction_factor_for_subsession(self.group.subsession)
        )

    def as_dict(self):
        return dict(
            price=str(self.price),
            quantity=str(self.quantity),
            quantity_initial=str(self.quantity_initial),
            round_statement=str(self.round_statement),
        )

    @classmethod
    def active_qs(cls, group, house):
        # returns a LIST of Bid objects
        return cls.filter(active=True, group=group, BTC_Statement=house)

    @classmethod
    def create_and_match(cls, *, player, BTC_Statement, price, quantity, round_statement):
        """
        oTree6 ExtraModel-compatible replacement for old signal logic.
        Call this instead of Bid.objects.create(...)
        """
        group = player.group
        price = float(price)
        quantity = int(quantity)
        round_statement = int(round_statement)

        # ---- pre-check: buyer has enough funds for total quantity
        total_cost = price * quantity
        if BTC_Statement is False:
            # Currency A: a frozen buyer has zero USABLE dollars this period and
            # is rejected outright (covers both posting a Dollar bid and the
            # "Accept best ask" path, which also creates a bid). Balance is left
            # untouched. Currency B is never affected by the freeze.
            if player.is_dollar_frozen():
                raise dollar_frozen_exception()
            # Currency A: never taxed, affordability unchanged.
            if player.allocation_dollar < total_cost:
                raise NotEnoughFunds(player, BTC_Statement, player.allocation_dollar)
        else:
            # Currency B: buyer ultimately pays P_hat / f, so require funds for
            # the post-tax payment (price * quantity / f), not the pre-tax price.
            f = btc_transaction_factor_for_subsession(player.subsession)
            if player.allocation_btc < total_cost / f:
                raise NotEnoughFunds(player, BTC_Statement, player.allocation_btc)

        # ---- deactivate previous bids from this player/house/round
        old_bids = cls.filter(
            active=True,
            group=group,
            player=player,
            BTC_Statement=BTC_Statement,
            round_statement=round_statement,
        )
        

        for b in old_bids:
            b.active = False


        # ---- create new bid
        bid = cls.create(
            group=group,
            player=player,
            BTC_Statement=BTC_Statement,
            price=price,
            quantity=quantity,
            quantity_initial=quantity,
            round_statement=round_statement,
            active=True,
            created_at=_now(),
        )

        # ---- match loop
        while bid.active and bid.quantity > 0:

            asks = Ask.filter(
                active=True,
                group=group,
                BTC_Statement=BTC_Statement,
                round_statement=round_statement,
            )
            
            

            # keep only asks at/under bid price
            asks = [a for a in asks if float(a.price) <= float(bid.price)]
            if not asks:
                break
                
            

            # pick best ask: lowest price, then earliest created_at
            asks.sort(key=lambda a: (float(a.price), float(a.created_at)))
            ask = asks[0]

            # ---- funds re-check (safety)
            # (for 1 unit at a time; Contract.create likely decrements balances)
            if bid.BTC_Statement is False:
                # Currency A: a frozen buyer has zero usable dollars (defense in
                # depth; a frozen buyer can never reach here because posting is
                # blocked above).
                if bid.player.is_dollar_frozen():
                    raise dollar_frozen_exception()
                # Currency A: untaxed, check against P_hat.
                if bid.player.allocation_dollar < float(bid.price):
                    raise NotEnoughFunds(bid.player, bid.BTC_Statement, bid.player.allocation_dollar)
            else:
                # Currency B: buyer pays P_hat / f, so check against P_hat / f.
                f = btc_transaction_factor_for_subsession(bid.player.subsession)
                if bid.player.allocation_btc < float(bid.price) / f:
                    raise NotEnoughFunds(bid.player, bid.BTC_Statement, bid.player.allocation_btc)

            # ---- seller provides item
            item = ask.player.item_to_sell(BTC_Statement)
            if not item:
                # seller ran out unexpectedly; stop
                break


            
            Contract.execute_trade(
                group=group,             # keep consistent with your Ask-side
                item=item,
                bid=bid,
                ask=ask,
                price=min(float(bid.price), float(ask.price)),
            )
        
        
        
        return bid



class Slot(ExtraModel):
    created_at = djmodels.DateTimeField(auto_now_add=True)
    updated_at = djmodels.DateTimeField(auto_now=True)
    owner = models.Link(Player)
    BTC = models.BooleanField()
    cost = models.FloatField(blank=True)
    value = models.FloatField(blank=True)


class Item(ExtraModel):
    created_at = djmodels.DateTimeField(auto_now_add=True)
    updated_at = djmodels.DateTimeField(auto_now=True)
    slot = models.Link(Slot)
    quantity = models.IntegerField()


class Contract(ExtraModel):
    created_at = djmodels.DateTimeField(auto_now_add=True)
    updated_at = djmodels.DateTimeField(auto_now=True)

    group = models.Link(Group)
    round_contract = models.IntegerField()
    item = models.Link(Item)
    bid = models.Link(Bid)
    ask = models.Link(Ask)
    price = models.FloatField()
    cost = models.FloatField()
    value = models.FloatField()

    # Explicit, auditable money flows for Currency B (bitcoin) trades.
    # For dollar (Currency A) trades these are: payment == receipt == price,
    # tax == 0. For BTC trades: buyer_payment_btc == P_hat / f,
    # seller_receipt_btc == P_hat, tax_paid_btc == P_hat / f - P_hat.
    buyer_payment_btc = models.FloatField(initial=0.0)
    seller_receipt_btc = models.FloatField(initial=0.0)
    tax_paid_btc = models.FloatField(initial=0.0)

    @property
    def price_incl_tax(self):
        """Display-only tax-inclusive price P_hat/f for this executed contract
        (see _btc_gross_price). None for Currency A (dollar) contracts. The stored
        price stays the pre-tax match price."""
        if not bool(getattr(self.bid, "BTC_Statement", False)):
            return None
        return _btc_gross_price(
            self.price, btc_transaction_factor_for_subsession(self.group.subsession)
        )

    def get_seller(self):
        return self.ask.player

    def get_buyer(self):
        return self.bid.player

    @classmethod
    def execute_trade(cls, group, item, bid, ask, price):

        buyer = bid.player
        seller = ask.player

        # move item to buyer slot
        cost = item.slot.cost or 0
        new_slot = buyer.get_free_slot(bid.BTC_Statement)
        
        if new_slot is None:
            raise Exception("Buyer has no free slot")       
        
        item.slot=new_slot
        value = new_slot.value or 0

        # persist contract (ExtraModel)
        contract = cls.create(
            group=group,
            round_contract=int(bid.round_statement),
            item=item,
            bid=bid,
            ask=ask,
            price=float(price),
            cost=float(cost),
            value=float(value),
        )
        

        # money transfer (per unit trade)
        P_hat = float(contract.price)  # auction price stays pre-tax
        if bid.BTC_Statement is False:
            # Currency A: untaxed. Buyer pays P_hat, seller receives P_hat.
            buyer.allocation_dollar -= P_hat
            seller.allocation_dollar += P_hat
            buyer.thisround_dollar -= P_hat
            seller.thisround_dollar += P_hat

            # Record flows for completeness/auditing (no tax on Currency A).
            contract.buyer_payment_btc = 0.0
            contract.seller_receipt_btc = 0.0
            contract.tax_paid_btc = 0.0
        else:
            # Currency B: buyer pays P_hat / f, seller receives P_hat. The
            # difference P_hat / f - P_hat is the tax, redistributed next period.
            f = btc_transaction_factor_for_subsession(group.subsession)
            buyer_payment = P_hat / f
            seller_receipt = P_hat
            tax = buyer_payment - seller_receipt

            buyer.allocation_btc -= buyer_payment
            seller.allocation_btc += seller_receipt
            buyer.thisround_btc -= buyer_payment
            seller.thisround_btc += seller_receipt

            # Store the asymmetric amounts explicitly so the tax never has to
            # be recomputed downstream.
            contract.buyer_payment_btc = buyer_payment
            contract.seller_receipt_btc = seller_receipt
            contract.tax_paid_btc = tax


        # decrement & persist ask quantity/active
        new_ask_q = max(int(ask.quantity) - 1, 0)
        ask.quantity=new_ask_q
        ask.active=(new_ask_q > 0)

        new_bid_q = max(int(bid.quantity) - 1, 0)
        bid.quantity=new_bid_q
        bid.active=(new_bid_q > 0)


        # build updates for oTree-live (NOT channels)
        player_updates = {}
        for p in [buyer, seller]:
            warnings = []

            # compute ex-ante/ex-post market activity
            exante_active_dollar = getattr(p, "active_dollar", False)
            p.active_dollar = p.is_active(False)
            expost_active_dollar = p.active_dollar
            if exante_active_dollar and (not expost_active_dollar):
                house_name = "A"
                things = "package" if p.role() == "seller" else "currency"
                warnings.append(
                    f"You have no {things} remaining in Market {house_name}. Market {house_name} is closed."
                )

            exante_active_btc = getattr(p, "active_btc", False)
            p.active_btc = p.is_active(True)
            expost_active_btc = p.active_btc
            if exante_active_btc and (not expost_active_btc):
                house_name = "B"
                things = "package" if p.role() == "seller" else "currency"
                warnings.append(
                    f"You have no {things} remaining in Market {house_name}. Market {house_name} is closed."
                )

            p.set_this_round_payoff()
            p.set_units(True)
            p.set_units(False)
            

            # player_updates[p.id_in_group] = {
                # "warning": warnings[-1] if warnings else None,
                # "repo": p.get_repo_html(False),
                # "repo_btc": p.get_repo_html(True),
                # "contracts": p.get_contracts_html(False),
                # "contracts_btc": p.get_contracts_html(True),
                # "form": p.get_form_html(False),
                # "form_btc": p.get_form_html(True),
                # "profit": p.profit_block_html(False),
                # "profit_btc": p.profit_block_html(True),
                # "general_info": p.general_info_block_html(),
                # "presence": p.presence_check(),
            # }

        # group_update = {"presence": group.presence_check()}

        return contract




























class IntroWp(WaitPage):
    group_by_arrival_time = True

    @staticmethod
    def is_displayed(player: Player):
        return player.group.is_active_round()
    
    def after_all_players_arrive(self):
        g = self.group
        sub = self.subsession

        g.set_role()
        g.set_total_round()
        g.set_sequence()

        # 0) dollar-freeze schedule for this period. Roles are now set, so this
        # only freezes buyers (sellers/disabled => always False). Done before the
        # trading interface is shown. The dollar BALANCE is never touched here;
        # only the per-period usability flag (dollar_frozen) is set.
        for p in g.get_players():
            p.compute_dollar_frozen()

        # 1) payoff bookkeeping
        for p in g.get_players():
            p.inherit_sequence_payoff()
            p.clear_sequence_payoff()

        # 2) carry over within-sequence balances
        if self.round_number != 1:
            for p in g.get_players():
                p.allocation_dollar = p.in_round(self.round_number - 1).allocation_dollar
                p.allocation_btc = p.in_round(self.round_number - 1).allocation_btc

        # 3) reset at start of each sequence
        if self.round_number in g.first_round_in_sequence():
            for p in g.get_players():
                if p.role() == "buyer":
                    p.allocation_dollar = C.BUYER_DOLLAR_ENDOWMENT
                    p.allocation_btc = C.BUYER_BTC_ENDOWMENT
                else:
                    p.allocation_dollar = 0.0
                    p.allocation_btc = 0.0
                p.allocation_dollar_save = 0.0
                p.allocation_btc_save = 0.0

        # 4) inflation + BTC tax redistribution (only when NOT sequence-start)
        #
        # Both run only in this branch, i.e. when the current round continues the
        # same sequence as the previous round. This guarantees that tax collected
        # in the final period of a sequence is NOT carried into the reset first
        # period of the next sequence (step 3 above performs the reset and skips
        # this branch entirely), satisfying the "redistribute within the same
        # sequence only" rule.
        else:
            if sub.inflation_on == 1:
                for p in g.get_players():
                    p.allocation_dollar = round(p.allocation_dollar * (1 + sub.inflation_rate), 2)

            # Redistribute the previous period's collected BTC tax equally to
            # every player in THIS group (the market that produced the tax).
            # Scope = group: bids/asks/contracts are all group-scoped, so each
            # group is an independent market; the tax it collects is returned to
            # its own participants. (If markets had to be pooled across multiple
            # groups, this would instead be done at the subsession level.)
            #
            # IntroWp uses group_by_arrival_time, so groups may be rearranged
            # between rounds and Group.in_round() is INVALID here (it raises
            # InvalidRoundError). We instead reach the previous round's group(s)
            # via Player.in_round(...).group, which is safe under GBAT (this is
            # the same pattern the balance carry-over in step 2 relies on). Group
            # membership is stable across rounds in this design (only roles
            # swap), so the current players normally share a single previous
            # group; we dedupe by group id to stay correct if they don't.
            players = sorted(g.get_players(), key=lambda p: p.id_in_group)
            number_of_players = len(players)

            prev_round = self.round_number - 1
            prev_groups = {}
            for p in players:
                prev_group = p.in_round(prev_round).group
                prev_groups[prev_group.id] = prev_group
            total_btc_tax = sum(
                pg.total_btc_tax_collected() for pg in prev_groups.values()
            )

            if total_btc_tax > 0 and number_of_players > 0:
                # Rounding rule: each player gets the equal share rounded to the
                # cent; any leftover residual (total - share * n, also to the
                # cent) is assigned to the lowest-id_in_group player so that the
                # amounts redistributed sum back exactly to the tax collected.
                share = round(total_btc_tax / number_of_players, 2)
                residual = round(total_btc_tax - share * number_of_players, 2)
                for p in players:
                    p.allocation_btc += share
                players[0].allocation_btc += residual

    
    
    
    

    # def after_all_players_arrive(self):
        # g = self.group
        # sub = self.subsession

        # g.set_role()
        # g.set_total_round()
        # g.set_sequence()
        # g.fake_draw()

        # for p in g.get_players():
            # p.inherit_sequence_payoff()
            # p.clear_sequence_payoff()

            # if self.round_number != 1:
                # for p in g.get_players():
                    # p.allocation_dollar = p.in_round(self.round_number - 1).allocation_dollar
                    # p.allocation_btc = p.in_round(self.round_number - 1).allocation_btc

            # if self.round_number in g.first_round_in_sequence():
                # if p.role() == "buyer":
                            # p.allocation_dollar = C.BUYER_DOLLAR_ENDOWMENT
                            # p.allocation_btc = C.BUYER_BTC_ENDOWMENT
                            # p.allocation_dollar_save = 0.0
                            # p.allocation_btc_save = 0.0
                # else:
                    # # sellers typically start with 0 currency (they start with items/units instead)
                    # p.allocation_dollar = 0.0
                    # p.allocation_btc = 0.0
                    # p.allocation_dollar_save = 0.0
                    # p.allocation_btc_save = 0.0

            # else:
                # if sub.inflation_on == 1:
                    # for p in g.get_players():
                        # p.allocation_dollar = round(p.allocation_dollar * (1 + sub.inflation_rate), 2)



class WorkPage(Page):
    form_model = "player"
    timer_text = "Time left to complete the task:"

    # We keep a superset here; we'll override via get_form_fields()
    form_fields = [
        "allocation_unit_dollar",
        "allocation_unit_btc",
        "allocation_dollar",
        "allocation_btc",
        "allocation_dollar_save",
        "allocation_btc_save",
    ]

    @staticmethod
    def get_timeout_seconds(player: Player):
        # Read from session config if it's a variable param; fallback to subsession field if you set it there
        cfg = player.subsession.session.config
        return int(cfg.get("time_per_round_currency", 0) or 0)

    @staticmethod
    def is_displayed(player: Player): 
        return player.group.is_active_round() and player.role() == "seller"
        # try to fixing 'skip' by returning true for all players

    @staticmethod
    def get_form_fields(player: Player):
        if player.role() == "seller":
            return ["allocation_unit_dollar", "allocation_unit_btc"]
        if player.role() == "buyer":
            return ["allocation_dollar", "allocation_btc", "allocation_dollar_save", "allocation_btc_save"]
        return []

    @staticmethod
    def vars_for_template(player: Player):
        # ---- Ensure defaults so template never touches None ----

        if player.role() == "seller":
            if player.field_maybe_none("allocation_unit_btc") is None:
                player.allocation_unit_btc = 0
            if player.field_maybe_none("allocation_unit_dollar") is None:
                player.allocation_unit_dollar = 0

        if player.role() == "buyer":
            if player.field_maybe_none("allocation_dollar") is None:
                player.allocation_dollar = 0
            if player.field_maybe_none("allocation_btc") is None:
                player.allocation_btc = 0
            if player.field_maybe_none("allocation_dollar_save") is None:
                player.allocation_dollar_save = 0
            if player.field_maybe_none("allocation_btc_save") is None:
                player.allocation_btc_save = 0


        return {
            # role label for templates (no filters)
            "role_label": player.role().capitalize(),

            # labels
            "allocation_unit_dollar_label": (
                "How many units will you allocate to Currency A auction house (from {} to {})?"
                .format(0, C.SELLER_ENDOWMENT)
            ),
            "allocation_dollar_label": (
                "How many dollars will you allocate to Currency A auction house (from {} to {})?"
                .format(0, C.BUYER_DOLLAR_ENDOWMENT)
            ),
            "allocation_unit_btc_label": (
                "Number of units to be sold in Currency B auction house (from {} to {})"
                .format(0, C.SELLER_ENDOWMENT)
            ),
            "allocation_btc_label": (
                "How many bitcoins will you allocate to Currency B auction house (from {} to {})?"
                .format(0, C.BUYER_BTC_ENDOWMENT)
            ),
            "allocation_dollar_save_label": "Amount of dollar remaining for saving",
            "allocation_btc_save_label": "Amount of bitcoins remaining for saving",

            # SAFE values for readonly inputs (avoid accessing possibly-None fields in template)
            "allocation_unit_btc_val": player.allocation_unit_btc or 0,
            "allocation_dollar_save_val": player.allocation_dollar_save or 0,
            "allocation_btc_save_val": player.allocation_btc_save or 0,

            # pass constants with your uppercase naming
            "seller_endowment": C.SELLER_ENDOWMENT,
            "buyer_dollar_endowment": C.BUYER_DOLLAR_ENDOWMENT,
            "buyer_btc_endowment": C.BUYER_BTC_ENDOWMENT,
        }


    @staticmethod
    def before_next_page(player: Player, timeout_happened):
        """
        Put state changes here (NOT in is_displayed).
        Also fix constant names to your uppercase convention.
        """
        # If you truly want to initialize buyers at the start of each sequence,
        # do it in a place that runs once per submit (or timeout).
        # Note: because this page is currently only displayed to sellers,
        # this "buyer init" will never run. If you need buyer init, move it to
        # a WaitPage after roles are set, or make this page displayed to everyone.
        if player.round_number in player.group.first_round_in_sequence():
            if player.role() == "buyer":
                player.allocation_dollar = C.BUYER_DOLLAR_ENDOWMENT
                player.allocation_dollar_save = 0
                player.allocation_btc = C.BUYER_BTC_ENDOWMENT
                player.allocation_btc_save = 0

        if timeout_happened:
            if player.role() == "buyer":
                player.allocation_dollar = C.BUYER_DOLLAR_ENDOWMENT
                player.allocation_dollar_save = 0
                player.allocation_btc = C.BUYER_BTC_ENDOWMENT
                player.allocation_btc_save = 0
            elif player.role() == "seller":
                player.allocation_unit_dollar = C.SELLER_ENDOWMENT
                player.allocation_unit_btc = 0


class GeneratingInitialsWP(WaitPage):

    @staticmethod
    def is_displayed(player: Player):
        return player.group.is_active_round()

    @staticmethod
    def after_all_players_arrive(group: Group):
        g = group
        cfg = g.subsession.session.config

        no_transaction_costs = int(cfg.get("no_transaction_costs", 0) or 0)
        units_per_buyer = int(cfg.get("units_per_buyer", 0) or 0)

        # Sellers: create slots/items for both auction houses
        for s in g.get_sellers():
            alloc_btc = s.field_maybe_none("allocation_unit_btc") or 0
            alloc_usd = s.field_maybe_none("allocation_unit_dollar") or 0

            for _ in range(int(alloc_btc)):
                slot = Slot.create(owner=s, cost=0, BTC=True)
                Item.create(slot=slot, quantity=C.INITIAL_QUANTITY)

            for _ in range(int(alloc_usd)):
                slot = Slot.create(owner=s, cost=0, BTC=False)
                Item.create(slot=slot, quantity=C.INITIAL_QUANTITY)

            s.set_units(True)
            s.set_units(False)

        # Buyers: create value slots
        for b in g.get_buyers():
            if no_transaction_costs == 0:
                for i in range(units_per_buyer):
                    Slot.create(owner=b, value=75, BTC=True)

                    if i <= 9:
                        Slot.create(owner=b, value=100 - i * 10, BTC=False)
                    elif i <= 18:
                        Slot.create(owner=b, value=10 - (i - 9) * 1, BTC=False)
                    else:
                        Slot.create(owner=b, value=1, BTC=False)
            else:
                for _ in range(units_per_buyer):
                    Slot.create(owner=b, value=100, BTC=True)
                    Slot.create(owner=b, value=100, BTC=False)

            b.allocation_unit_btc = 0
            b.allocation_unit_dollar = 0
            b.set_units(True)
            b.set_units(False)


# -------------------------------
# HELPERS FOR SENDING DATA TO UI
# -----------------------------

def _serialize_stmt_for(p, s, f=None, house=False):
    return {
        "id": s.id,
        "price": float(s.price) if s.price is not None else None,
        "quantity": int(s.quantity) if s.quantity is not None else None,
        "is_me": (s.player_id == p.id),
        # Display-only tax-inclusive price for the "Incl. tax" column; None for
        # Currency A so the dollar market renders exactly as before.
        "incl_tax": _btc_gross_price(s.price, f) if house else None,
    }



def _stmt_to_dict(stmt):
    if not stmt:
        return None
    return {
        "price": float(stmt.price),
        "quantity": float(stmt.quantity),
        "round_statement": int(getattr(stmt, "round_statement", 0)),
        "BTC_Statement": bool(getattr(stmt, "BTC_Statement", False)),
    }
    
    
def _profit_to_dict(p):
    """
    Return only primitive JSON-safe values for the profit block.
    """
    if p.role() == "buyer":
        result =  {
            "allocation_dollar": p.allocation_dollar,
            "allocation_btc": p.allocation_btc,
            "units_dollar": p.units_dollar,
            "units_btc": p.units_btc,
            "thisround_profit": p.thisround_profit,
            "thisround_euro": p.thisround_euro
        }

    else:
        result = { 
            "allocation_dollar": p.allocation_dollar,
            "allocation_btc": p.allocation_btc,
            "units_dollar": p.units_dollar,
            "units_btc": p.units_btc
        } 


    return result
        
def _contracts_to_dict(p, house):
    """
    Return only primitive JSON-safe values for the contracts section of the info block.
    """

    contracts = p.get_contracts(house) or []

    # newest first
    contracts.sort(
        key=lambda c: (getattr(c, "created_at", 0) or getattr(c, "id", 0)),
        reverse=True
    )

    # Display-only tax factor for the Currency B (bitcoin) contracts table.
    f = btc_transaction_factor_for_subsession(p.subsession) if house else None

    result = []

    for c in contracts:
        # Display-only tax-inclusive price for the "Incl. tax" column; None for
        # Currency A. See _btc_gross_price.
        incl_tax = _btc_gross_price(c.price, f) if house else None
        if p.role() == "buyer":
            result.append({
                "item_quantity": c.item.quantity,
                "value": c.value,
                "price": c.price,
                "incl_tax": incl_tax,
            })

        else:
            result.append({
                "item_quantity": c.item.quantity,
                "price": c.price,
                "incl_tax": incl_tax,
            })
            
 
    return result
     
    
    
def live_market(player: Player, data):
    g = player.group
    data = data or {}
    action = data.get("action")
    house = bool(data.get("BTC_Statement", False))  # False=dollar, True=btc
    
    # -----------------------
    # 1) Apply action
    # -----------------------
    if action == "new_statement":
        try:
            price = float(data.get("price"))
            quantity = int(data.get("quantity"))
        except (TypeError, ValueError):
            return {player.id_in_group: {"error": "Invalid price/quantity"}}

        if price <= 0 or quantity <= 0:
            return {player.id_in_group: {"error": "Price and quantity must be > 0"}}

        # IMPORTANT: use your matching entrypoints
        try:
            if player.role() == "buyer":
                Bid.create_and_match(
                    player=player,
                    BTC_Statement=house,
                    price=price,
                    quantity=quantity,
                    round_statement=player.round_number,
                )
            else:
                Ask.create_and_match(
                    player=player,
                    BTC_Statement=house,
                    price=price,
                    quantity=quantity,
                    round_statement=player.round_number,
                )
        except Exception as e:
            return {player.id_in_group: {"error": str(e)}}

    elif action == "retract_statement":
        stmts = (
            Bid.filter(player=player, active=True, BTC_Statement=house)
            if player.role() == "buyer"
            else Ask.filter(player=player, active=True, BTC_Statement=house)
        )
        if stmts:
            stmts.sort(key=lambda s: (getattr(s, "created_at", 0) or 0), reverse=True)
            last_stmt = stmts[0]
            last_stmt.active = False

    elif action == "best_statement":

        try:
            if player.role() == "buyer":
                # Buyer accepts the BEST ASK (lowest ask) in this house
                best = g.best_ask(house)
                if not best:
                    return {player.id_in_group: {"error": "No asks available to accept."}}

                Bid.create_and_match(
                    player=player,
                    BTC_Statement=house,
                    price=float(best.price),      
                    quantity=1,                  
                    round_statement=player.round_number,
                )

            else:
                # Seller accepts the BEST BID (highest bid) in this house
                best = g.best_bid(house)
                if not best:
                    return {player.id_in_group: {"error": "No bids available to accept."}}

                Ask.create_and_match(
                    player=player,
                    BTC_Statement=house,
                    price=float(best.price),      
                    quantity=1,                  
                    round_statement=player.round_number,
                )

        except Exception as e:
            return {player.id_in_group: {"error": str(e)}}

    elif action == "refresh":
        pass

    else:
        return  # ignore unknown action

    # -----------------------
    # 2) Build per-player payloads
    # -----------------------
    asks_d = g.get_asks(False)
    bids_d = g.get_bids(False)
    asks_b = g.get_asks(True)
    bids_b = g.get_bids(True)

    # Display-only tax factor for Currency B (bitcoin) price suffixes.
    f_btc = btc_transaction_factor_for_subsession(g.subsession)


    # -----------------------
    # 3) Build rich per-player payloads
    # -----------------------
    out = {}
    for p in g.get_players():
        # recompute and (optionally) persist activity flags
        p.active_dollar = p.is_active(False)
        p.active_btc = p.is_active(True)

       
        # update payoff/units so the blocks reflect current state
        p.set_this_round_payoff()
        p.set_units(False)
        p.set_units(True)

        # form_state booleans (you already have this helper)
        ctx_d = p.get_form_context(False)
        

        out[p.id_in_group] = {
            # existing stuff:
            "group_presence": g.presence_check(),
            "orderbook": {
                "dollar": {
                    "asks": [_serialize_stmt_for(p, a) for a in asks_d],
                    "bids": [_serialize_stmt_for(p, b) for b in bids_d],
                },
                "btc": {
                    "asks": [_serialize_stmt_for(p, a, f_btc, True) for a in asks_b],
                    "bids": [_serialize_stmt_for(p, b, f_btc, True) for b in bids_b],
                },
            },
            "form_state": {
                "no_slots_or_funds_dollar": bool(ctx_d.get("no_slots_or_funds_dollar")),
                "no_slots_or_funds_btc": bool(ctx_d.get("no_slots_or_funds_btc")),
            },
            
            "last_statement": _stmt_to_dict(p.get_last_statement()),
            "last_statement_btc": _stmt_to_dict(p.get_last_statement_btc()),
            "profit": _profit_to_dict(p),
            "contracts": _contracts_to_dict(p, False),
            "contracts_btc": _contracts_to_dict(p, True),
            "currency_symbol": p.Currency_symbol(),
            # Whether to render the display-only "Incl. tax" column (BTC tables).
            "tax_on": bool(f_btc < 1.0),
        }
       
            
    return out






class Market(Page):
    
    live_method = live_market

    @staticmethod
    def get_timeout_seconds(player: Player):
        cfg = player.subsession.session.config
        return int(cfg.get("time_per_round", 0) or 0)

    @staticmethod
    def is_displayed(player: Player):
        g = player.group
        cfg = player.subsession.session.config

        if not g.is_active_round():
            return False

        currency_exchange_on = int(cfg.get("currency_exchange_on", 0) or 0)

        if g.After_CE is False:
            return True

        # g.After_CE is True
        if currency_exchange_on == 0:
            return False

        # currency_exchange_on == 1
        player_active = bool(getattr(player, "active_btc", False) or getattr(player, "active_dollar", False))
        return player_active and g.active

    @staticmethod
    def vars_for_template(player: Player):
        g = player.group

        ctx = player.get_form_context(True)

        # these methods must already be oTree6-safe (no .objects joins etc.)
        ctx["asks"] = g.get_asks(False)
        ctx["bids"] = g.get_bids(False)
        ctx["asks_btc"] = g.get_asks(True)
        ctx["bids_btc"] = g.get_bids(True)

        ctx["repository"] = player.get_repo_context(False)
        ctx["repository_btc"] = player.get_repo_context(True)

        ctx["contracts"] = player.get_contracts_queryset(False)
        ctx["contracts_btc"] = player.get_contracts_queryset(True)

        # participant index is optional; safe access:
        ctx["page_index"] = getattr(player.participant, "_index_in_pages", None)

        Euro = [{"data": [], "name": "Earnings in " + player.Currency_name()}]
        for i in range(1, 1001):
            Euro[0]["data"].append(round((i) ** 0.5 / 10, 2))
        ctx["Euro"] = Euro

        return ctx

    @staticmethod
    def before_next_page(player: Player, timeout_happened):
        player.thisround_btc = 0
        player.thisround_dollar = 0
        





class ResultsWaitPage4(WaitPage):
    body_text = "Waiting for the other participant to decide."

    @staticmethod
    def is_displayed(player: Player):
        return player.group.is_active_round()

    @staticmethod
    def after_all_players_arrive(group: Group):
        group.clear_ask_bid()
        for p in group.get_players():
            p.set_sequence_payoff()
            p.set_payoff()



class SequencePage(Page):
    timer_text = "Time left to complete the task:"

    @staticmethod
    def is_displayed(player: Player):
        return player.group.is_active_round() and player.group.is_end_of_block()

    @staticmethod
    def get_timeout_seconds(player: Player):
        return player.subsession.drop_player_time

    @staticmethod
    def before_next_page(player: Player, timeout_happened):
        if timeout_happened:
            player.no_action1 = True



class Dropout(Page):
    timer_text = "Time Remaining before dropping out of the experiment"
    form_model = "player"
    form_fields = []

    @staticmethod
    def get_timeout_seconds(player: Player):
        return player.subsession.drop_player_time

    @staticmethod
    def is_displayed(player: Player):
        return (
            player.group.is_active_round()
            and player.no_action1 is True
            and player.subsession.drop_player_on == 1
        )

    @staticmethod
    def before_next_page(player: Player, timeout_happened):
        if timeout_happened:
            player.drop_one()


class ResultsWaitPage5(WaitPage):
    body_text = "Waiting for the other participant to decide."

    @staticmethod
    def is_displayed(player: Player):
        return player.group.is_active_round()


class Termination(Page):
    timer_text = "Time left to complete the task:"

    @staticmethod
    def is_displayed(player: Player):
        if player.disconnection_flag != 0:
            player.set_payment_message()
            return True
        return False

    @staticmethod
    def app_after_this_page(player: Player, upcoming_apps):
        if player.disconnection_flag != 0:
            return upcoming_apps[0]



class PaymentPage(Page):
    timer_text = "Time left to complete the task:"

    @staticmethod
    def is_displayed(player: Player):
        if player.group.is_active_round() and player.round_number == player.group.total_round:
            player.set_payment_message()
            return True
        return False

    @staticmethod
    def app_after_this_page(player: Player, upcoming_apps):
        if player.round_number == player.group.total_round:
            return upcoming_apps[0]








def creating_session(subsession: Subsession):
    validate_sequence_configuration(subsession.session)
    subsession.set_config()
    if subsession.session.num_participants % (subsession.num_buyers + subsession.num_sellers) != 0:
        raise Exception("Number of participants is not divisible by number of sellers and buyers")
        

page_sequence = [
    IntroWp,
    WorkPage,
    GeneratingInitialsWP,
    Market,
    ResultsWaitPage4,
    SequencePage,
    Dropout,
    ResultsWaitPage5,
    Termination,
    PaymentPage,
]

