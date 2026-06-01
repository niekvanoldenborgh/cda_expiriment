
# double_auction_v2/exceptions.py

class MarketException(Exception):
    """Base class for market-related errors."""
    def __init__(self, message: str, payload: dict | None = None):
        super().__init__(message)
        self.payload = payload or {}


class NotEnoughFunds(MarketException):
    def __init__(self, owner, house, endowment):
        house_name = "B" if house is True else "A"
        msg = f"Not enough money to create a new bid of this amount (Market {house_name})."
        payload = {
            "warning": (
                "You do not have enough funds to make this bid. "
                f"You have <b>{endowment} Currency {house_name}</b>."
            )
        }
        super().__init__(msg, payload)


class NotEnoughItemsToSell(MarketException):
    def __init__(self, owner, house, units):
        house_name = "B" if house is True else "A"
        msg = f"Not enough items to sell (Market {house_name})."
        payload = {
            "warning": (
                "You do not have enough items to make this ask. "
                f"You have <b>{units}</b> packages in Market <b>{house_name}</b>."
            )
        }
        super().__init__(msg, payload)


class NoEndowment(MarketException):
    def __init__(self, owner, house):
        house_name = "Bitcoin" if house is True else "Dollar"
        msg = f"No endowment in {house_name} auction house."
        payload = {
            "warning": f"You have no {house_name} in {house_name} auction house. {house_name} auction house is closed."
        }
        super().__init__(msg, payload)


class NoItems(MarketException):
    def __init__(self, owner, house):
        house_name = "Bitcoin" if house is True else "Dollar"
        msg = f"No items remaining in {house_name} auction house."
        payload = {
            "warning": f"You have no package remaining in {house_name} auction house. {house_name} auction house is closed."
        }
        super().__init__(msg, payload)

