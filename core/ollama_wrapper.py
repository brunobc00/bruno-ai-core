import ollama
import logging

# Configuração básica de logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class OllamaWrapper:
    """
    Wrapper simples para interagir com o Ollama rodando localmente na GPU.
    """
    def __init__(self, chat_model="llama3.1:8b", embed_model="nomic-embed-text"):
        self.chat_model = chat_model
        self.embed_model = embed_model

    def ask(self, prompt: str) -> str:
        try:
            logger.info(f"Enviando prompt para {self.chat_model}...")
            response = ollama.chat(model=self.chat_model, messages=[
                {'role': 'user', 'content': prompt},
            ])
            return response['message']['content']
        except Exception as e:
            logger.error(f"Erro ao chamar Ollama (chat): {e}")
            return f"Erro: {e}"

    def get_embedding(self, text: str):
        try:
            response = ollama.embeddings(model=self.embed_model, prompt=text)
            return response['embedding']
        except Exception as e:
            logger.error(f"Erro ao gerar embedding: {e}")
            return None

if __name__ == "__main__":
    # Teste rápido de sanidade
    core = OllamaWrapper()
    print("--- Teste de Chat ---")
    print(core.ask("Olá! Você está usando a GPU local? Responda em uma frase curta."))
    
    print("\n--- Teste de Embedding ---")
    emb = core.get_embedding("Teste de vetorização")
    if emb:
        print(f"Embedding gerado com sucesso! Tamanho: {len(emb)}")
