# Bruno AI Core - Guia do Projeto

Este repositório é o núcleo de inteligência pessoal, responsável por gerenciar a conexão com a GPU local (AMD RX 7600) e fornecer ferramentas genéricas de IA para outros projetos.

## Missão
Prover uma camada de abstração estável para LLMs locais, mantendo a privacidade e a eficiência.

## Estrutura
- `core/`: Scripts base de conexão (Ollama).
- `data/`: Armazenamento de vetores (ChromaDB) e caches locais.
- `.venv/`: Ambiente virtual com as bibliotecas de IA.

## Comandos Úteis
- Testar conexão: `source .venv/bin/activate && python3 core/ollama_wrapper.py`
- Instalar novas ferramentas: `pip install <pacote>`

## Configurações de Modelos
- Chat: `llama3.1:8b`
- Embeddings: `nomic-embed-text`
