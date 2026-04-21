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

## Protocolo de Trabalho (Estimativa & Aprovação)

Antes de iniciar qualquer tarefa, o Antigravity deve apresentar um quadro de estimativa e aguardar a aprovação explícita do usuário:

| Recurso | Descrição |
| :--- | :--- |
| **Inteligência** | Antigravity (Cloud) vs Ollama (Local GPU) |
| **Monetização** | Estimativa de créditos/tokens consumidos (Google Billing) |
| **Recursos Locais** | Uso estimado de VRAM e processamento local |
| **Tempo** | Prazo estimado para conclusão |

## Configurações de Modelos
- Chat: `llama3.1:8b`
- Embeddings: `nomic-embed-text`

## Git e Atualização do Hub

Após qualquer alteração neste repositório (scripts, docs, hub):

```bash
git add . && git commit -m "<tipo>: <descrição>" && git push origin main
```

Quando arquivos `.md` de instrução forem alterados no workspace, executar `update_hub.py` para refletir no hub antes do commit:

```bash
python3 update_hub.py
```
