# bruno-ai-core — Regras do Projeto

## Propósito

Repositório central de infraestrutura de IA local. Contém o wrapper do Ollama e scripts de análise de repositórios.

---

## Regra Principal: Ollama é o Motor de Trabalho Braçal

**QUALQUER agente** que operar neste workspace deve seguir esta hierarquia:

1. **Agente (você)**: Lógica, planejamento, criação de código, edição de arquivos, Git.
2. **Ollama (llama3.1:8b)**: Leitura de documentos, interpretação de texto, avaliação de conformidade, extração de dados de PDFs.

Se uma tarefa envolve **ler e interpretar** conteúdo (PDF, texto longo, normas), **não faça isso inline**. Use o Ollama.

---

## Como usar o OllamaWrapper

```python
import sys
sys.path.insert(0, "/home/bruno/Documentos/Github/bruno-ai-core")
from core.ollama_wrapper import OllamaWrapper

llm = OllamaWrapper(chat_model="llama3.1:8b")
resposta = llm.ask("Seu prompt aqui")
print(resposta)
```

## Modelos disponíveis

| Modelo | Uso recomendado |
|---|---|
| `llama3.1:8b` | Análise de documentos, avaliação técnica, texto em PT |
| `qwen2.5:7b` | Alternativa geral, multilingual |
| `qwen2.5-coder:1.5b-base` | Geração de código simples |
| `nomic-embed-text` | Vetorização para busca semântica |

---

## Estrutura

```
bruno-ai-core/
  core/
    ollama_wrapper.py   ← wrapper principal — não modificar sem necessidade
  scripts/
    analyze_repo.py     ← análise de repositórios via LLM
```

---

## Git

```bash
git add . && git commit -m "<tipo>: <descrição>" && git push origin main
```
