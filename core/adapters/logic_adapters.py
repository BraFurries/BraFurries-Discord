from chatterbot.logic import LogicAdapter
from random import choice

class NoRepeatAdapter(LogicAdapter):
    def __init__(self, chatbot, **kwargs):
        super().__init__(chatbot, **kwargs)
        self.last_response = None

    def can_process(self, statement):
        return True

    def process(self, statement):
        response = self.generate_response(statement.text)
        self.last_response = response
        return response

    def generate_response(self, input_text):
        if self.last_response and self.last_response.text != "bom, eu não fui treinado pra nenhuma outra resposta pra isso então...":
            response = "a unica resposta que eu consigo te dar é " + self.last_response.text
        else:
            response = "bom, eu não fui treinado pra nenhuma outra resposta pra isso então..."
        
        return self.chatbot.storage.tagger.get_statement(response)

    def get_default_response(self, input_statement):
        if self.last_response:
            return self.last_response
        return super().get_default_response(input_statement)