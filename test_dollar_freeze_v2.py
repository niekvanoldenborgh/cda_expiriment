"""
Tests for the Dollar (Currency A) freeze feature in `double_auction_v2`.

Boots a real in-memory oTree runtime and drives the REAL model methods
(`Bid.create_and_match`, `Ask.create_and_match`, `Contract.execute_trade`,
`IntroWp.after_all_players_arrive`, `Player.compute_dollar_frozen`, the freeze
helpers). Operates entirely in memory; db.sqlite3 is never written.

Run:
    OTREE_IN_MEMORY=1 OTREE_PRODUCTION=0 .venv/Scripts/python.exe test_dollar_freeze_v2.py
"""
import os
os.environ.setdefault("OTREE_IN_MEMORY", "1")
os.environ.setdefault("OTREE_PRODUCTION", "0")

import sys
from types import SimpleNamespace
sys.path.insert(0, os.getcwd())

import otree.main
otree.main.setup()

import otree.session as S
from otree.database import db, session_scope
import double_auction_v2 as M

CONFIG = "double_auction_market_v2"


# ----------------------------------------------------------------------------
# harness helpers
# ----------------------------------------------------------------------------
def new_session(**fields):
    with session_scope():
        sess = S.create_session(
            CONFIG, num_participants=2, modified_session_config_fields=fields
        )
        sid = sess.id
    db.new_session()
    return sid


def round_objs(sid, rnd):
    ss = db.query(M.Subsession).filter_by(session_id=sid, round_number=rnd).one()
    g = ss.get_groups()[0]
    players = {p.id_in_group: p for p in g.get_players()}
    return ss, g, players


def setup_market(g, players, house, buyer_alloc, price, rnd=1, post_ask=True):
    """id_in_group 1 = buyer (with funds + free slot), 2 = seller (with a unit)."""
    buyer, seller = players[1], players[2]
    buyer.role_code = "buyer"
    seller.role_code = "seller"
    buyer.allocation_dollar = buyer_alloc if house is False else 0.0
    buyer.allocation_btc = buyer_alloc if house is True else 0.0
    seller.allocation_dollar = 0.0
    seller.allocation_btc = 0.0
    sl = M.Slot.create(owner=seller, cost=0.0, BTC=house)
    M.Item.create(slot=sl, quantity=1)
    seller.set_units(house)
    M.Slot.create(owner=buyer, value=75.0, BTC=house)
    db.commit()
    if post_ask:
        M.Ask.create_and_match(player=seller, BTC_Statement=house, price=price,
                               quantity=1, round_statement=rnd)
        db.commit()
    return buyer, seller


def run_intro(sid, rnd):
    ss, g, _ = round_objs(sid, rnd)
    fake_self = SimpleNamespace(group=g, subsession=ss, round_number=rnd)
    M.IntroWp.after_all_players_arrive(fake_self)
    db.commit()


def frozen_map_over_rounds(sid, T):
    """Run IntroWp for rounds 1..T in order.

    Returns {(id_in_group, round): (role_code, frozen, sequence_index)}.
    The sequence index lets tests distinguish the training sequence (index 0,
    never frozen) from the paid sequences.
    """
    out = {}
    for t in range(1, T + 1):
        run_intro(sid, t)
        ss, _, players = round_objs(sid, t)
        seq = M.current_sequence_index_for_round(ss.session, t)
        for idig, p in players.items():
            out[(idig, t)] = (p.role_code, bool(p.field_maybe_none("dollar_frozen")), seq)
    return out


# ----------------------------------------------------------------------------
# test runner
# ----------------------------------------------------------------------------
RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond)))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def approx(a, b):
    return abs(float(a) - float(b)) < 1e-6


# ----------------------------------------------------------------------------
# 1. Reproducibility of the pure draw (no group/session id in the key)
# ----------------------------------------------------------------------------
def test_reproducibility_pure_helper():
    seed, prob = 999, 0.37
    grid = [(i, t) for i in range(1, 5) for t in range(1, 21)]
    sched_a = {(i, t): M.is_dollar_frozen_for(seed, i, t, prob) for i, t in grid}
    sched_b = {(i, t): M.is_dollar_frozen_for(seed, i, t, prob) for i, t in grid}
    check("Reproducibility: same seed -> identical schedule (helper, twice)",
          sched_a == sched_b)

    # a different seed should generally produce a different schedule
    sched_c = {(i, t): M.is_dollar_frozen_for(seed + 1, i, t, prob) for i, t in grid}
    check("Reproducibility: different seed -> different schedule", sched_a != sched_c)

    # underlying uniform draw is itself reproducible
    check("Reproducibility: uniform draw is deterministic",
          approx(M.dollar_freeze_draw(seed, 3, 9), M.dollar_freeze_draw(seed, 3, 9)))


# ----------------------------------------------------------------------------
# 2. Reproducibility across two independent sessions with the same seed
#    (different sessions => same schedule for the same buyer positions)
# ----------------------------------------------------------------------------
def test_reproducibility_across_sessions():
    # Span the training sequence (rounds 1..15, index 0) AND paid sequence 1
    # (rounds 16+), so the non-trivial-mix assertion exercises real draws.
    T = 30
    sid1 = new_session(dollar_freeze_enabled=True, freeze_probability=0.5, freeze_seed=42)
    map1 = frozen_map_over_rounds(sid1, T)
    sid2 = new_session(dollar_freeze_enabled=True, freeze_probability=0.5, freeze_seed=42)
    map2 = frozen_map_over_rounds(sid2, T)
    check("Reproducibility: two sessions, same seed -> identical frozen map",
          map1 == map2, f"keys={len(map1)}")

    # training sequence (index 0) is always unfrozen regardless of the draw
    training_frozen = [fr for (_role, fr, seq) in map1.values()
                       if seq == M.TRAINING_SEQUENCE_INDEX and fr]
    check("Reproducibility: training sequence never frozen",
          len(training_frozen) == 0, f"training-frozen count={len(training_frozen)}")

    # sanity: in the paid sequence, with prob 0.5 the schedule is non-trivial
    paid_buyer_frozen = [fr for (role, fr, seq) in map1.values()
                         if role == "buyer" and seq != M.TRAINING_SEQUENCE_INDEX]
    check("Reproducibility: paid-sequence schedule is non-trivial (mix of frozen/unfrozen)",
          any(paid_buyer_frozen) and not all(paid_buyer_frozen))

    # a different seed should give a different map (very likely)
    sid3 = new_session(dollar_freeze_enabled=True, freeze_probability=0.5, freeze_seed=7)
    map3 = frozen_map_over_rounds(sid3, T)
    check("Reproducibility: different seed -> different frozen map", map1 != map3)


# ----------------------------------------------------------------------------
# 3. Sellers are never frozen, even with probability = 1
# ----------------------------------------------------------------------------
def test_sellers_never_frozen():
    # Cover training (1..15) and paid sequence 1 (16..18) so the probability=1
    # claim is checked only where the freeze applies.
    sid = new_session(dollar_freeze_enabled=True, freeze_probability=1.0, freeze_seed=5)
    fm = frozen_map_over_rounds(sid, 18)
    seller_frozen = [fr for (role, fr, _seq) in fm.values() if role == "seller" and fr]
    paid_buyer_all_frozen = all(
        fr for (role, fr, seq) in fm.values()
        if role == "buyer" and seq != M.TRAINING_SEQUENCE_INDEX
    )
    check("Sellers never frozen (even with probability=1)", len(seller_frozen) == 0,
          f"seller-frozen count={len(seller_frozen)}")
    check("probability=1 => every PAID-sequence buyer frozen every period",
          paid_buyer_all_frozen)


# ----------------------------------------------------------------------------
# 3b. Training sequence (index 0) is gated out: never frozen, even at prob=1,
#     while the first paid sequence IS frozen at prob=1.
# ----------------------------------------------------------------------------
def test_training_sequence_gate():
    sid = new_session(dollar_freeze_enabled=True, freeze_probability=1.0, freeze_seed=5)
    fm = frozen_map_over_rounds(sid, 18)  # rounds 1..15 training, 16..18 paid seq 1

    training_buyer = [fr for (role, fr, seq) in fm.values()
                      if role == "buyer" and seq == M.TRAINING_SEQUENCE_INDEX]
    paid_buyer = [fr for (role, fr, seq) in fm.values()
                  if role == "buyer" and seq != M.TRAINING_SEQUENCE_INDEX]

    check("Training gate: buyer-rounds exist in both training and paid sequences",
          len(training_buyer) > 0 and len(paid_buyer) > 0,
          f"training={len(training_buyer)}, paid={len(paid_buyer)}")
    check("Training gate: no training-sequence buyer frozen (even at prob=1)",
          not any(training_buyer))
    check("Training gate: all paid-sequence buyers frozen (prob=1)",
          all(paid_buyer))


# ----------------------------------------------------------------------------
# 4. probability=0 -> never frozen; probability=1 -> always frozen (buyers)
# ----------------------------------------------------------------------------
def test_probability_bounds():
    grid = [(i, t) for i in range(1, 4) for t in range(1, 11)]
    none_frozen = all(not M.is_dollar_frozen_for(123, i, t, 0.0) for i, t in grid)
    all_frozen = all(M.is_dollar_frozen_for(123, i, t, 1.0) for i, t in grid)
    check("probability=0 -> no buyer ever frozen", none_frozen)
    check("probability=1 -> every buyer always frozen", all_frozen)


# ----------------------------------------------------------------------------
# 5. Feature disabled -> never frozen even with probability=1, dollar trade works
# ----------------------------------------------------------------------------
def test_disabled_no_freeze():
    sid = new_session(dollar_freeze_enabled=False, freeze_probability=1.0, freeze_seed=5)
    run_intro(sid, 1)
    _, g, players = round_objs(sid, 1)
    any_frozen = any(bool(p.field_maybe_none("dollar_frozen")) for p in players.values())
    check("Disabled: no player frozen even with probability=1", not any_frozen)

    # and a normal dollar trade still goes through
    buyer, seller = setup_market(g, players, False, 100.0, 40.0, rnd=1)
    M.Bid.create_and_match(player=buyer, BTC_Statement=False, price=40.0, quantity=1, round_statement=1)
    db.commit()
    traded = len(M.Contract.filter(group=g)) == 1
    check("Disabled: dollar trade executes normally", traded and approx(buyer.allocation_dollar, 60.0),
          f"contracts={len(M.Contract.filter(group=g))}, alloc={buyer.allocation_dollar}")


# ----------------------------------------------------------------------------
# 6. Frozen buyer cannot POST a dollar bid (backend rejects), balance preserved
# ----------------------------------------------------------------------------
def test_frozen_cannot_post_dollar_bid():
    sid = new_session(dollar_freeze_enabled=True, freeze_probability=1.0, freeze_seed=5)
    _, g, players = round_objs(sid, 1)
    buyer, seller = setup_market(g, players, False, 100.0, 40.0, rnd=1)
    buyer.dollar_frozen = True  # explicit, independent of RNG
    db.commit()
    rejected = False
    try:
        M.Bid.create_and_match(player=buyer, BTC_Statement=False, price=40.0, quantity=1, round_statement=1)
    except M.MarketException:
        rejected = True
    db.commit()
    no_trade = len(M.Contract.filter(group=g)) == 0
    check("Frozen buyer: dollar bid rejected by backend", rejected and no_trade,
          f"rejected={rejected}, contracts={len(M.Contract.filter(group=g))}")
    check("Frozen buyer: dollar balance preserved (not zeroed) on rejection",
          approx(buyer.allocation_dollar, 100.0), f"alloc={buyer.allocation_dollar}")


# ----------------------------------------------------------------------------
# 7. Frozen buyer cannot ACCEPT a dollar ask (same Bid.create_and_match path)
# ----------------------------------------------------------------------------
def test_frozen_cannot_accept_dollar_ask():
    sid = new_session(dollar_freeze_enabled=True, freeze_probability=1.0, freeze_seed=5)
    _, g, players = round_objs(sid, 1)
    buyer, seller = setup_market(g, players, False, 100.0, 40.0, rnd=1)
    buyer.dollar_frozen = True
    db.commit()
    # "Accept best ask" routes through Bid.create_and_match at the best ask price
    best = g.best_ask(False)
    rejected = False
    try:
        M.Bid.create_and_match(player=buyer, BTC_Statement=False, price=float(best.price),
                               quantity=1, round_statement=1)
    except M.MarketException:
        rejected = True
    db.commit()
    check("Frozen buyer: accepting a dollar ask is rejected",
          rejected and len(M.Contract.filter(group=g)) == 0)


# ----------------------------------------------------------------------------
# 8. Frozen buyer can STILL post & match BTC trades normally
# ----------------------------------------------------------------------------
def test_frozen_btc_unaffected():
    sid = new_session(dollar_freeze_enabled=True, freeze_probability=1.0, freeze_seed=5,
                      btc_transaction_factor=1.0)
    _, g, players = round_objs(sid, 1)
    buyer, seller = setup_market(g, players, True, 100.0, 50.0, rnd=1)
    buyer.dollar_frozen = True  # dollars frozen, BTC must be unaffected
    db.commit()
    M.Bid.create_and_match(player=buyer, BTC_Statement=True, price=50.0, quantity=1, round_statement=1)
    db.commit()
    traded = len(M.Contract.filter(group=g)) == 1
    check("Frozen buyer: BTC trade still executes", traded,
          f"contracts={len(M.Contract.filter(group=g))}")
    check("Frozen buyer: BTC balances move normally (f=1)",
          approx(buyer.allocation_btc, 50.0) and approx(seller.allocation_btc, 50.0),
          f"buyer_btc={buyer.allocation_btc}, seller_btc={seller.allocation_btc}")


# ----------------------------------------------------------------------------
# 9. Dollar balance preserved during a frozen period, usable again when unfrozen
# ----------------------------------------------------------------------------
def test_balance_preserved_then_reusable():
    sid = new_session(dollar_freeze_enabled=True, freeze_probability=1.0, freeze_seed=5)

    # Round 1: frozen -> dollar bid rejected, balance untouched
    _, g1, players1 = round_objs(sid, 1)
    buyer1, seller1 = setup_market(g1, players1, False, 100.0, 40.0, rnd=1)
    buyer1.dollar_frozen = True
    db.commit()
    try:
        M.Bid.create_and_match(player=buyer1, BTC_Statement=False, price=40.0, quantity=1, round_statement=1)
    except M.MarketException:
        pass
    db.commit()
    check("Reusable: balance unchanged after a frozen period",
          approx(buyer1.allocation_dollar, 100.0), f"alloc={buyer1.allocation_dollar}")

    # Round 2: SAME buyer position, now NOT frozen -> dollar trade succeeds
    _, g2, players2 = round_objs(sid, 2)
    # make id_in_group 1 a buyer again with the carried balance, unfrozen
    buyer2, seller2 = setup_market(g2, players2, False, buyer1.allocation_dollar, 40.0, rnd=2)
    buyer2.dollar_frozen = False
    db.commit()
    M.Bid.create_and_match(player=buyer2, BTC_Statement=False, price=40.0, quantity=1, round_statement=2)
    db.commit()
    check("Reusable: dollars usable again in a non-frozen period",
          len(M.Contract.filter(group=g2)) == 1 and approx(buyer2.allocation_dollar, 60.0),
          f"contracts={len(M.Contract.filter(group=g2))}, alloc={buyer2.allocation_dollar}")


# ----------------------------------------------------------------------------
# 10. BTC tax identical whether or not the buyer's dollars are frozen
# ----------------------------------------------------------------------------
def test_tax_identical_under_freeze():
    f, P = 0.8, 50.0

    def run(frozen):
        sid = new_session(dollar_freeze_enabled=True, freeze_probability=1.0,
                          freeze_seed=5, btc_transaction_factor=f)
        _, g, players = round_objs(sid, 1)
        buyer, seller = setup_market(g, players, True, 100.0, P, rnd=1)
        buyer.dollar_frozen = frozen
        db.commit()
        b0, s0 = buyer.allocation_btc, seller.allocation_btc
        M.Bid.create_and_match(player=buyer, BTC_Statement=True, price=P, quantity=1, round_statement=1)
        db.commit()
        c = M.Contract.filter(group=g)[0]
        return (b0 - buyer.allocation_btc, seller.allocation_btc - s0,
                c.buyer_payment_btc, c.seller_receipt_btc, c.tax_paid_btc)

    frozen_res = run(True)
    unfrozen_res = run(False)
    check("Tax: BTC tax/flows identical whether dollars frozen or not",
          all(approx(a, b) for a, b in zip(frozen_res, unfrozen_res)),
          f"frozen={frozen_res}, unfrozen={unfrozen_res}")
    check("Tax: with frozen dollars, BTC tax still = P/f - P",
          approx(frozen_res[4], P / f - P), f"tax={frozen_res[4]}")


def main():
    tests = [
        test_reproducibility_pure_helper,
        test_reproducibility_across_sessions,
        test_sellers_never_frozen,
        test_training_sequence_gate,
        test_probability_bounds,
        test_disabled_no_freeze,
        test_frozen_cannot_post_dollar_bid,
        test_frozen_cannot_accept_dollar_ask,
        test_frozen_btc_unaffected,
        test_balance_preserved_then_reusable,
        test_tax_identical_under_freeze,
    ]
    for t in tests:
        print(f"\n=== {t.__name__} ===")
        try:
            t()
        except Exception as e:
            import traceback
            traceback.print_exc()
            check(t.__name__ + " (no exception)", False, str(e))

    passed = sum(1 for _, ok in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n================ {passed}/{total} checks passed ================")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
