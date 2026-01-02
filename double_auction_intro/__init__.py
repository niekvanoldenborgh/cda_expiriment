from otree.api import *


def euro_series(player):
    Euro = [{'data': [], 'name': 'Earnings in ' + player.Currency_name()}]
    for i in range(1, 1001):
        Euro[0]['data'].append(round((i ** 0.5) / 10, 2))
    return Euro


class C(BaseConstants):
    NAME_IN_URL = 'double_auction_intro'
    PLAYERS_PER_GROUP = None
    NUM_ROUNDS = 1

    INSTRUCTIONS_TEMPLATE = 'double_auction_intro/Instructions.html'


class Subsession(BaseSubsession):
    pass


class Group(BaseGroup):
    pass


class Player(BasePlayer):
    Q1_1 = models.IntegerField(label="")
    Q1_2 = models.FloatField(label="")

    Q2 = models.IntegerField(
        label="",
        choices=[
            [1, "No transaction is possible."],
            [2, "The transaction takes place and you sell to that buyer 2 packages at 10 units of currency A."],
            [3, "The transaction takes place and you sell to that buyer 2 packages at 18 units of currency A."],
            [4, "The transaction takes place and you sell to that buyer 3 packages at 10 units of currency A."],
            [5, "The transaction takes place and you sell to that buyer 3 packages at 18 units of currency A."],
            [6, "We do not have enough information to say whether a transaction can take place."],
        ],
    )

    Q4 = models.IntegerField(
        label="",
        choices=[
            [1, "The total quantities of currency A and currency B remain constant throughout each sequence"],
            [2, "The total quantity of currency A remains constant but the total quantity of Currency B increases by 30% in each period of a given sequence."],
            [3, "The total quantity of currency B remains constant but the total quantity of Currency A increases by 30% in each period of a given sequence."],
            [4, "The total quantities of currencies A and B decrease throughout each sequence."],
        ],
    )

    def Currency_symbol(self):
        country = self.session.config.get('experiment_country', '').upper()
        if country == 'CAD':
            return '$'
        if country == 'EUR':
            return '€'
        return ''

    def Currency_name(self):
        country = self.session.config.get('experiment_country', '').upper()
        if country == 'CAD':
            return 'Canadian dollar'
        if country == 'EUR':
            return 'Euro'
        return ''

    def Currency_contact_info(self):
        country = self.session.config.get('experiment_country', '').upper()
        if country == 'CAD':
            return 'email address'
        if country == 'EUR':
            return 'IBAN'
        return ''

    def inflation_info(self):
        return "" if self.session.config.get('inflation_on') == 1 else "hidden"

    def no_transaction_hidden(self):
        return "hidden" if self.session.config.get('no_transaction_costs') == 1 else ""

    def no_transaction_show(self):
        return "" if self.session.config.get('no_transaction_costs') == 1 else "hidden"

    def video_hidden(self):
        return "hidden" if self.session.config.get('no_video_intro') == 1 else ""


class Introduction0(Page):
    form_model = 'player'


class Introduction(Page):
    form_model = 'player'

    @staticmethod
    def vars_for_template(player: Player):
        return dict(Euro=euro_series(player))


class Video(Page):
    form_model = 'player'


class Quiz(Page):
    form_model = 'player'
    form_fields = ['Q1_1', 'Q1_2', 'Q2', 'Q4']

    @staticmethod
    def error_message(player: Player, values):
        Q1_1 = "Question 1(a) "
        Q1_2 = "Question 1(b) "
        Q2 = "Question 2 "
        Q4 = "Question 3 "

        if player.session.config.get('no_transaction_costs') == 0:
            if values['Q1_1'] == 345:
                Q1_1 = ""
            if values['Q1_2'] == 1.86:
                Q1_2 = ""
            if values['Q2'] == 2:
                Q2 = ""
        else:
            if values['Q1_1'] == 400:
                Q1_1 = ""
            if values['Q1_2'] == 2:
                Q1_2 = ""
            if values['Q2'] == 2:
                Q2 = ""

        if player.session.config.get('inflation_on') == 0:
            if values['Q4'] == 1:
                Q4 = ""
        else:
            if values['Q4'] == 3:
                Q4 = ""

        if (Q1_1 + Q1_2 + Q2 + Q4) != "":
            return (
                "The Answers for the following questions are not correct: "
                "{}{}{}{}. Please read the instructions and try again."
            ).format(Q1_1, Q1_2, Q2, Q4)

    @staticmethod
    def vars_for_template(player: Player):
        return dict(Euro=euro_series(player))


page_sequence = [Introduction0, Introduction, Quiz]
# If you want Video in the flow:
# page_sequence = [Introduction0, Introduction, Video, Quiz]


