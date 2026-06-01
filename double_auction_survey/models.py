from otree.api import (
    models, widgets, BaseConstants, BaseSubsession, BaseGroup, BasePlayer,
    Currency as c, currency_range
)

import random


doc = """
This application provides a webpage instructing participants how to get paid.
Examples are given for the lab and Amazon Mechanical Turk (AMT).
"""


class Constants(BaseConstants):
    name_in_url = 'double_auction_survey'
    players_per_group = None
    num_rounds = 1





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

    Q8 = models.StringField(label="")

    Q8_1 = models.StringField(label="")

    Q9 = models.StringField(label="Could you, in few words, summarize your strategy(ies) in this experiment?",blank=True)

    Q10 = models.StringField(label="If you would like to leave any comments for us, please do so here:",blank=True)

    Q11 = models.StringField(label="Please leave us your BIC/SWIFT for non-Dutch bank account:",blank=True)

    Q11_1 = models.StringField(label="Please type your BIC/SWIFT again:",blank=True)

