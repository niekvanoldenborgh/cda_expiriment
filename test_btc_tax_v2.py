"""
Tests for the Currency B (bitcoin) transaction tax in the `double_auction_v2`
app.

These tests boot a real in-memory oTree runtime, create real sessions (with the
new `btc_transaction_factor` overridden per test via
`modified_session_config_fields`) and drive the REAL model methods
(`Bid.create_and_match`, `Ask.create_and_match`, `Contract.execute_trade`, the
`IntroWp.after_all_players_arrive` redistribution branch and
`Group.total_btc_tax_collected`).

Run with:
    OTREE_IN_MEMORY=1 OTREE_PRODUCTION=0 .venv/Scripts/python.exe test_btc_tax_v2.py

OTREE_IN_MEMORY=1 makes everything operate on an in-memory copy of the DB; the
real db.sqlite3 file is never written (we never call save_sqlite_db()).
"""
import os
os.environ.setdefault("OTREE_IN_MEMORY", "1")
os.environ.setdefault("OTREE_PRODUCTION", "0")

import sys
sys.path.insert(0, os.getcwd())

import otree.main
otree.main.setup()

import otree.session as S
from otree.database import db, session_scope
import double_auction_v2 as M

CONFIG = "double_auction_market_v2"
EPS = 1e-9


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------
def new_session(factor):
    """Create a fresh real session with the given btc_transaction_factor."""
    with session_scope():
        sess = S.create_session(
            CONFIG,
            num_participants=2,
            modified_session_config_fields={"btc_transaction_factor": factor},
        )
        sid = sess.id
    db.new_session()  # fresh identity map for the rest of the test
    return sid


def round_objs(sid, rnd):
    """Return (subsession, group, {id_in_group: player}) for a given round."""
    ss = db.query(M.Subsession).filter_by(session_id=sid, round_number=rnd).one()
    g = ss.get_groups()[0]
    players = {p.id_in_group: p for p in g.get_players()}
    return ss, g, players


def setup_one_unit_market(g, players, house, buyer_alloc, price, rnd=1):
    """
    Give id_in_group 1 the buyer role with `buyer_alloc` currency and a free
    slot, and id_in_group 2 the seller role with one sellable unit, then have
    the seller post an ask at `price` so a later matching bid will trade.
    Returns (buyer, seller).
    """
    buyer = players[1]
    seller = players[2]
    buyer.role_code = "buyer"
    seller.role_code = "seller"

    # balances
    buyer.allocation_dollar = buyer_alloc if house is False else 0.0
    buyer.allocation_btc = buyer_alloc if house is True else 0.0
    seller.allocation_dollar = 0.0
    seller.allocation_btc = 0.0

    # seller gets one unit to sell in this house
    sl = M.Slot.create(owner=seller, cost=0.0, BTC=house)
    M.Item.create(slot=sl, quantity=1)
    seller.set_units(house)

    # buyer needs a free (value) slot to receive the item
    M.Slot.create(owner=buyer, value=75.0, BTC=house)

    db.commit()

    # seller posts the ask up-front (no bids yet -> no match)
    M.Ask.create_and_match(
        player=seller, BTC_Statement=house, price=price, quantity=1, round_statement=rnd
    )
    db.commit()
    return buyer, seller


# ----------------------------------------------------------------------------
# test runner
# ----------------------------------------------------------------------------
RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  -- {detail}" if detail else ""))


def approx(a, b):
    return abs(float(a) - float(b)) < 1e-6


# ----------------------------------------------------------------------------
# 1. f = 1 -> identical to no-tax behavior (BTC)
# ----------------------------------------------------------------------------
def test_f1_no_tax_btc():
    sid = new_session(1)
    _, g, players = round_objs(sid, 1)
    P = 50.0
    buyer, seller = setup_one_unit_market(g, players, True, 100.0, P)
    M.Bid.create_and_match(
        player=buyer, BTC_Statement=True, price=P, quantity=1, round_statement=1
    )
    db.commit()
    c = M.Contract.filter(group=g)[0]
    check("f=1 BTC: buyer pays P_hat", approx(buyer.allocation_btc, 100.0 - P),
          f"alloc={buyer.allocation_btc}")
    check("f=1 BTC: seller receives P_hat", approx(seller.allocation_btc, P),
          f"alloc={seller.allocation_btc}")
    check("f=1 BTC: tax == 0", approx(c.tax_paid_btc, 0.0), f"tax={c.tax_paid_btc}")
    check("f=1 BTC: total tax collected == 0", approx(g.total_btc_tax_collected(), 0.0))


# ----------------------------------------------------------------------------
# 2. Currency A is never taxed (even with f < 1)
# ----------------------------------------------------------------------------
def test_currency_a_untaxed():
    sid = new_session(0.5)  # heavy tax, must not touch Currency A
    _, g, players = round_objs(sid, 1)
    P = 40.0
    buyer, seller = setup_one_unit_market(g, players, False, 100.0, P)
    M.Bid.create_and_match(
        player=buyer, BTC_Statement=False, price=P, quantity=1, round_statement=1
    )
    db.commit()
    c = M.Contract.filter(group=g)[0]
    check("Currency A: buyer pays P_hat (untaxed)", approx(buyer.allocation_dollar, 100.0 - P),
          f"alloc={buyer.allocation_dollar}")
    check("Currency A: seller receives P_hat", approx(seller.allocation_dollar, P),
          f"alloc={seller.allocation_dollar}")
    check("Currency A: tax_paid_btc == 0", approx(c.tax_paid_btc, 0.0))
    check("Currency A: total BTC tax collected == 0", approx(g.total_btc_tax_collected(), 0.0))


# ----------------------------------------------------------------------------
# 3. BTC trade with f < 1: buyer pays P/f, seller gets P, diff == recorded tax
# ----------------------------------------------------------------------------
def test_btc_tax_split():
    f = 0.8
    sid = new_session(f)
    _, g, players = round_objs(sid, 1)
    P = 50.0
    buyer, seller = setup_one_unit_market(g, players, True, 100.0, P)
    buyer_before = buyer.allocation_btc
    seller_before = seller.allocation_btc
    M.Bid.create_and_match(
        player=buyer, BTC_Statement=True, price=P, quantity=1, round_statement=1
    )
    db.commit()
    c = M.Contract.filter(group=g)[0]

    buyer_decrease = buyer_before - buyer.allocation_btc
    seller_increase = seller.allocation_btc - seller_before

    check("BTC: buyer decreases by P/f", approx(buyer_decrease, P / f),
          f"decrease={buyer_decrease}, expected={P/f}")
    check("BTC: seller increases by P", approx(seller_increase, P),
          f"increase={seller_increase}")
    check("BTC: recorded buyer_payment_btc == P/f", approx(c.buyer_payment_btc, P / f))
    check("BTC: recorded seller_receipt_btc == P", approx(c.seller_receipt_btc, P))
    check("BTC: recorded tax_paid_btc == P/f - P", approx(c.tax_paid_btc, P / f - P))
    check("BTC: buyer_decrease - seller_increase == tax", approx(buyer_decrease - seller_increase, c.tax_paid_btc))
    check("BTC: contract.price stays pre-tax (P_hat)", approx(c.price, P), f"price={c.price}")
    check("BTC: total tax collected == tax", approx(g.total_btc_tax_collected(), P / f - P))


# ----------------------------------------------------------------------------
# 4. Affordable at P_hat but NOT at P_hat/f -> rejected at posting time (BTC)
#    and the same balance IS accepted for Currency A.
# ----------------------------------------------------------------------------
def test_affordability_rejected_btc():
    f = 0.8
    P = 50.0
    # P/f = 62.5; pick a balance strictly between P and P/f
    alloc = 60.0

    # BTC: must be rejected
    sid = new_session(f)
    _, g, players = round_objs(sid, 1)
    buyer, seller = setup_one_unit_market(g, players, True, alloc, P)
    rejected = False
    try:
        M.Bid.create_and_match(
            player=buyer, BTC_Statement=True, price=P, quantity=1, round_statement=1
        )
    except M.NotEnoughFunds:
        rejected = True
    db.commit()
    no_trade = len(M.Contract.filter(group=g)) == 0
    check("BTC: bid affordable at P_hat but not P_hat/f is rejected", rejected and no_trade,
          f"rejected={rejected}, contracts={len(M.Contract.filter(group=g))}")

    # Currency A: same balance & price must be accepted (untaxed)
    sid2 = new_session(f)
    _, g2, players2 = round_objs(sid2, 1)
    buyer2, seller2 = setup_one_unit_market(g2, players2, False, alloc, P)
    a_ok = True
    try:
        M.Bid.create_and_match(
            player=buyer2, BTC_Statement=False, price=P, quantity=1, round_statement=1
        )
    except M.NotEnoughFunds:
        a_ok = False
    db.commit()
    a_traded = len(M.Contract.filter(group=g2)) == 1
    check("Currency A: same balance/price is accepted (untaxed)", a_ok and a_traded,
          f"ok={a_ok}, contracts={len(M.Contract.filter(group=g2))}")


# ----------------------------------------------------------------------------
# 5. Tax collected in a period == tax redistributed at start of next period.
#    Drives the REAL IntroWp.after_all_players_arrive at round 2 (same sequence).
# ----------------------------------------------------------------------------
def _run_intro_aapa(sid, rnd):
    """
    Invoke the REAL IntroWp.after_all_players_arrive for the given round.

    oTree exposes Page.group/.subsession as read-only properties, so we call the
    underlying function with a duck-typed `self` that exposes exactly the three
    attributes the method reads (group, subsession, round_number). The method
    operates on the real Group/Subsession objects via that self.
    """
    from types import SimpleNamespace
    ss, g, _ = round_objs(sid, rnd)
    fake_self = SimpleNamespace(group=g, subsession=ss, round_number=rnd)
    M.IntroWp.after_all_players_arrive(fake_self)
    db.commit()


def test_redistribution_next_period():
    f = 0.8
    sid = new_session(f)
    # ---- round 1: produce a real BTC tax of P/f - P = 12.5
    _, g1, players1 = round_objs(sid, 1)
    P = 50.0
    buyer1, seller1 = setup_one_unit_market(g1, players1, True, 100.0, P)
    # set round-1 roles so round-2 set_role can read them
    M.Bid.create_and_match(player=buyer1, BTC_Statement=True, price=P, quantity=1, round_statement=1)
    db.commit()
    total_tax = g1.total_btc_tax_collected()

    # capture round-1 end balances per participant code (1=buyer,2=seller)
    b1_btc = buyer1.allocation_btc   # 100 - 62.5 = 37.5
    s1_btc = seller1.allocation_btc  # 0 + 50 = 50.0

    # ---- run the real redistribution branch at round 2 (same sequence)
    _run_intro_aapa(sid, 2)

    _, g2, players2 = round_objs(sid, 2)
    share = round(total_tax / 2, 2)
    p1 = players2[1].allocation_btc
    p2 = players2[2].allocation_btc

    distributed = (p1 - b1_btc) + (p2 - s1_btc)
    check("Redistribution: total distributed == total collected",
          approx(distributed, total_tax), f"distributed={distributed}, collected={total_tax}")
    check("Redistribution: each player got the equal share (even split)",
          approx(p1 - b1_btc, share) and approx(p2 - s1_btc, share),
          f"p1+={p1-b1_btc}, p2+={p2-s1_btc}, share={share}")


# ----------------------------------------------------------------------------
# 6. Uneven split -> stated rounding rule (residual to lowest id_in_group),
#    redistributed amounts still sum exactly to the collected tax.
#    Uses a manually-crafted round-1 contract with an odd-cent tax.
# ----------------------------------------------------------------------------
def test_redistribution_rounding_remainder():
    f = 0.9
    sid = new_session(f)
    _, g1, players1 = round_objs(sid, 1)
    players1[1].role_code = "buyer"
    players1[2].role_code = "seller"
    # round-1 balances (so carryover is well-defined)
    players1[1].allocation_btc = 20.0
    players1[2].allocation_btc = 30.0

    # craft a contract whose tax does not divide evenly across 2 players
    odd_tax = 10.01
    sl_s = M.Slot.create(owner=players1[2], cost=0.0, BTC=True)
    it = M.Item.create(slot=sl_s, quantity=1)
    bid = M.Bid.create(group=g1, player=players1[1], BTC_Statement=True, price=10.0,
                       quantity=1, quantity_initial=1, round_statement=1, active=False,
                       created_at=M._now())
    ask = M.Ask.create(group=g1, player=players1[2], BTC_Statement=True, price=10.0,
                       quantity=1, quantity_initial=1, round_statement=1, active=False,
                       created_at=M._now())
    M.Contract.create(group=g1, round_contract=1, item=it, bid=bid, ask=ask,
                      price=10.0, cost=0.0, value=75.0,
                      buyer_payment_btc=10.0 + odd_tax, seller_receipt_btc=10.0,
                      tax_paid_btc=odd_tax)
    db.commit()

    total_tax = g1.total_btc_tax_collected()
    b1 = players1[1].allocation_btc
    s1 = players1[2].allocation_btc

    _run_intro_aapa(sid, 2)
    _, g2, players2 = round_objs(sid, 2)

    inc1 = players2[1].allocation_btc - b1
    inc2 = players2[2].allocation_btc - s1
    share = round(total_tax / 2, 2)
    residual = round(total_tax - share * 2, 2)

    check("Rounding: collected tax is odd-cent (10.01)", approx(total_tax, odd_tax),
          f"collected={total_tax}")
    check("Rounding: redistributed sum == collected (to the cent)",
          approx(round(inc1 + inc2, 2), round(total_tax, 2)),
          f"sum={round(inc1+inc2,2)}, collected={round(total_tax,2)}")
    check("Rounding: residual goes to lowest id_in_group (player 1)",
          approx(inc1, share + residual) and approx(inc2, share),
          f"inc1={inc1} (share+res={share+residual}), inc2={inc2} (share={share})")


# ----------------------------------------------------------------------------
# 7. Sequence reset: tax from the last period of a sequence is NOT carried into
#    the reset first period of the next sequence. Round 16 is the first round of
#    sequence 1, so balances reset to endowments with no tax added.
# ----------------------------------------------------------------------------
def test_sequence_reset_no_tax_carry():
    f = 0.8
    sid = new_session(f)
    # produce tax in round 15 (last visible round of sequence 0)
    _, g15, players15 = round_objs(sid, 15)
    P = 50.0
    buyer15, seller15 = setup_one_unit_market(g15, players15, True, 100.0, P, rnd=15)
    M.Bid.create_and_match(player=buyer15, BTC_Statement=True, price=P, quantity=1, round_statement=15)
    db.commit()
    tax15 = g15.total_btc_tax_collected()

    # also set round-15 roles already done; run round 16 (sequence-1 first round)
    _run_intro_aapa(sid, 16)
    _, g16, players16 = round_objs(sid, 16)

    # at sequence start: buyers reset to endowment, sellers to 0; NO tax added.
    buyer_btc = players16[1].allocation_btc
    seller_btc = players16[2].allocation_btc
    endow = M.C.BUYER_BTC_ENDOWMENT

    # one of the two players is the buyer at round 16; both must be either
    # exactly the endowment or exactly 0 (never endowment+share or 0+share).
    vals = sorted([buyer_btc, seller_btc])
    reset_clean = approx(vals[0], 0.0) and approx(vals[1], endow)
    check("Sequence reset: round-15 produced tax (>0)", tax15 > 0, f"tax15={tax15}")
    check("Sequence reset: round-16 balances are clean endowment/0 (no tax carried)",
          reset_clean, f"buyer_btc={buyer_btc}, seller_btc={seller_btc}, endow={endow}")


def main():
    tests = [
        test_f1_no_tax_btc,
        test_currency_a_untaxed,
        test_btc_tax_split,
        test_affordability_rejected_btc,
        test_redistribution_next_period,
        test_redistribution_rounding_remainder,
        test_sequence_reset_no_tax_carry,
    ]
    for t in tests:
        print(f"\n=== {t.__name__} ===")
        try:
            t()
        except Exception as e:
            import traceback
            traceback.print_exc()
            check(t.__name__ + " (no exception)", False, str(e))

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n================ {passed}/{total} checks passed ================")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
