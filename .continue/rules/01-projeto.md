# bruno-ai-core — Regras do Projeto

## Propósito

Repositório central de infraestrutura de IA local. Contém o wrapper do Ollama e scripts de análise.
**Regras globais do workspace**: ver `/home/bruno/Documentos/Github/WORKSPACE_RULES.md`

---

## Regra Principal: Maximizar Uso da GPU

O Ollama está configurado para rodar na GPU local. **Sempre priorizar** modelos maiores quando
a tarefa for complexa, e deixar o modelo processar sem interrupção.

### Verificar GPU

```bash
ollama ps        # mostra modelos carregados e uso de GPU
nvidia-smi       # mostra uso atual da placa
```

---

## OllamaWrapper — Uso Correto

```python
import sys
sys.path.insert(0, "/home/bruno/Documentos/Github/bruno-ai-core")
from core.ollama_wrapper import OllamaWrapper

# Para análise de documentos (PT-BR, textos longos):
llm = OllamaWrapper(chat_model="llama3.1:8b")
resultado = llm.ask("Seu prompt aqui")

# Para embeddings:
llm = OllamaWrapper(embed_model="nomic-embed-text")
vetor = llm.get_embedding("texto para vetorizar")
```

### Dicas para maximizar GPU:
- Não rodar múltiplas instâncias em paralelo.
- Para textos longos, dividir em chunks de ~4000 chars antes de enviar.

---

## Modelos Disponíveis

| Modelo | Tamanho | Uso ideal |
|---|---|---|
| `llama3.1:8b` | 4.9 GB | Análise de documentos, texto PT-BR, avaliação técnica |
| `qwen2.5:7b` | 4.7 GB | Multilingual, tarefas gerais |
| `qwen2.5-coder:1.5b-base` | 986 MB | Geração rápida de código |
| `nomic-embed-text` | 274 MB | Embeddings/busca semântica |

---

## Estrutura

```
bruno-ai-core/
  core/
    ollama_wrapper.py   <- não modificar a interface pública sem necessidade
  scripts/
    analyze_repo.py     <- análise de repositórios via LLM
```

---

## Git

```bash
git add . && git commit -m "<tipo>: <descrição>" && git push origin main
```
