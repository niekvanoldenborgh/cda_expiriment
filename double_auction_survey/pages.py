from ._builtin import Page, WaitPage
from otree.api import Currency as c, currency_range
from .models import Constants


class Survey(Page):


    form_model = 'player'
    form_fields = ['Q1','Q2','Q2_1','Q3','Q4','Q4_1','Q5','Q6','Q7', 'Q8','Q8_1','Q9','Q10','Q11','Q11_1']

    def error_message(self, values):

        if values['Q8']!=values['Q8_1']:
            return 'Your '+ self.player.Currency_contact_info() + ' do not match.'





page_sequence = [Survey]

