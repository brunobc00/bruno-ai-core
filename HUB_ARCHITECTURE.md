# 🛸 Antigravity Hub - Arquitetura e Visão Geral

Este documento serve como guia mestre para qualquer desenvolvedor (ou IA) que assumir o projeto do Hub de Documentação Técnica.

## 🎯 Objetivo do Projeto
Transformar o repositório de normas técnicas da CEMIG em uma plataforma interativa, onde o usuário pode consultar qualquer detalhe técnico através de um chat inteligente que "lê" os manuais em tempo real.

---

## 🏗️ Estrutura do Sistema

### 1. O Frontend (Docs Hub)
- **Localização**: `/docs/hub/index.html`
- **Tecnologias**: HTML5, Vanilla CSS, JS Puro.
- **Diferencial**: Interface SPA (Single Page Application) com design premium (Glassmorphism). 
- **Chat Inteligente**: Envia perguntas para o backend local e recebe a resposta da IA junto com os trechos das normas que foram usados para gerar aquela resposta.

### 2. O Servidor (O "Cérebro")
- **Localização**: `/server/main.py`
- **Tecnologias**: FastAPI (Python), HTTPX.
- **Lógica de Busca (RAG Simples)**:
    - O servidor monitora palavras-chave (ex: "individual", "63A", "ND 5.1").
    - Quando detectadas, ele abre os arquivos `.md` na pasta `/cemig/normas/`.
    - Ele realiza uma busca por Regex para encontrar as linhas exatas da tabela ou texto.
    - Ele injeta esses trechos como "Contexto" no prompt do Ollama.

### 3. O Motor de IA (Ollama)
- **Modelo**: `llama3.1:8b` (Local).
- **Embedding**: `nomic-embed-text` (Preparado para futuras buscas vetoriais).
- **Acesso**: O servidor FastAPI serve como uma ponte segura entre o navegador e o Ollama.

---

## 🔐 Segurança e Acesso
- **Túnel**: O acesso externo é feito via **Cloudflare Tunnel**.
- **Autenticação**: Protegido por **Cloudflare Zero Trust** (Google OAuth).
- **Acesso Local**: O servidor roda na porta `8000` e o Ollama na `11434`.

---

## 🚀 Fluxo de Trabalho (Como manter o site vivo)

### Iniciar o Projeto:
1.  **Backend**: `cd /bruno-ai-core && python3 server/main.py`
2.  **Túnel**: `docker run --net=host cloudflare/cloudflared tunnel run --token <TOKEN>`

### Atualizar Normas:
1.  Sempre que uma norma nova for adicionada em `/cemig/normas/`, ela deve estar em formato **.md** para que o servidor consiga ler.

---

## 🔮 Próximos Passos (Backlog)
- [ ] **Busca Vetorial (ChromaDB)**: Substituir a busca por Regex por uma busca semântica real usando os embeddings do `nomic-embed-text`.
- [ ] **Multi-Agentes**: Implementar seleção de modelos (Claude, Gemini, Ollama) no chat.
- [ ] **Interface de Gestão**: Uma página para fazer upload de novas normas e rodar o script de conversão PDF -> MD automaticamente.

---
**Desenvolvido por Bruno & Antigravity AI**
