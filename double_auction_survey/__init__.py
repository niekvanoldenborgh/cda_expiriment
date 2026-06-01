from otree.api import *


doc = """
Post-experiment survey (oTree 6).
Collects questionnaire + payout contact info (e.g., IBAN for EUR or email for CAD).
"""


class C(BaseConstants):
    NAME_IN_URL = "survey"
    PLAYERS_PER_GROUP = None
    NUM_ROUNDS = 1


class Subsession(BaseSubsession):
    pass


class Group(BaseGroup):
    pass


class Player(BasePlayer):
    Q1 = models.StringField(label="Age:")
    Q2 = models.StringField(label="Nationality:")
    Q2_1 = models.StringField(label="Your place of residence:")
    Q3 = models.StringField(label="Gender",choices=[
        ['M', 'Male'],
        ['F', 'Female'],
        ['O', 'Other'],
    ])

    Q4 = models.StringField(label="Which of the following comes closest to your field of study?",choices=[
        ['Economics, Business', 'Economics, Business'],
        ['Psychology, Social Sciences, Law, Humanities', 'Psychology, Social Sciences, Law, Humanities'],
        ['Mathematics, Physics, IT', 'Mathematics, Physics, IT'],
        ['Medicine, Biology, Chemistry', 'Medicine, Biology, Chemistry'],
        ['Other', 'Other'],
    ])

    Q4_1 = models.StringField(label="Please specify, if you choose other",blank=True)

    Q5 = models.IntegerField(label="How would you describe your command of English, on a Scale of 1-5?",choices=[
        [5,'5 - Excellent' ],
        [4,'4 - Very good' ],
        [3,'3 - Good' ],
        [2,'2 - Mediocre' ],
        [1,'1 - Bad' ],
    ])

    Q6 = models.StringField(label="Do you think that the other participants in your group understood the instructions well?",choices=[
        ['Yes', 'Yes'],
        ['No', 'No'],
    ])

    Q7 = models.StringField(label="Have you participated in a similar trading experiment before?",choices=[
        ['Yes', 'Yes'],
        ['No', 'No'],
    ])

    def Currency_contact_info(self):
        if self.session.config['experiment_country'].upper() == 'CAD':
            # return dollar sign
            return 'email address'
        elif self.session.config['experiment_country'].upper() == 'EUR':
            # return euro sign
            return 'IBAN'

    def Currency_name(self):
        if self.session.config['experiment_country'].upper() == 'CAD':
            # return dollar sign
            return 'Canadian dollar'
        elif self.session.config['experiment_country'].upper() == 'EUR':
            # return euro sign
            return 'Euro'

    Q8 = models.StringField(label="", blank=True)

    Q8_1 = models.StringField(label="", blank=True)

    Q9 = models.StringField(label="Could you, in few words, summarize your strategy(ies) in this experiment?",blank=True)

    Q10 = models.StringField(label="If you would like to leave any comments for us, please do so here:",blank=True)

    Q11 = models.StringField(label="Please leave us your BIC/SWIFT for non-Dutch bank account:", blank=True)

    Q11_1 = models.StringField(label="Please type your BIC/SWIFT again:", blank=True)



class Survey(Page):
    form_model = 'player'

    @staticmethod
    def get_form_fields(player):
        fields = [
            'Q1', 'Q2', 'Q2_1', 'Q3', 'Q4', 'Q4_1',
            'Q5', 'Q6', 'Q7', 'Q8', 'Q8_1', 'Q9', 'Q10'
        ]
        if player.Currency_name() == 'Euro':
            fields += ['Q11', 'Q11_1']
        return fields

    @staticmethod
    def error_message(player, values):
        q8 = values.get('Q8').strip()
        q8_1 = values.get('Q8_1').strip()
        q11 = values.get('Q11').strip()
        q11_1 = values.get("Q11_1").strip()
        
        # If not Euro, just require Q8/Q8_1 (email) to be filled & match
        if player.Currency_name() != 'Euro':
            if not q8 or not q8_1:
                return "Please enter your email address twice."
            if q8 != q8_1:
                return "Your email addresses do not match."
            return
           
        filled_iban = bool(q8 or q8_1)
        filled_bic  = bool(q11 or q11_1)

        if not filled_iban and not filled_bic:
            return "Please provide either your IBAN (twice) or your BIC/SWIFT (twice)."

        # optional: disallow providing both
        if filled_iban and filled_bic:
            return "Please provide either IBAN or BIC/SWIFT, not both."

        # If they chose IBAN, require both and matching
        if filled_iban:
            if not q8 or not q8_1:
                return "Please enter your IBAN twice."
            if q8 != q8_1:
                return "Your IBAN entries do not match."

        # If they chose BIC, require both and matching
        if filled_bic:
            if not q11 or not q11_1:
                return "Please enter your BIC/SWIFT twice."
            if q11 != q11_1:
                return "Your BIC/SWIFT entries do not match."


page_sequence = [Survey]


