from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
import httpx
import os

app = FastAPI(title="Antigravity Portal")

# Configurações
OLLAMA_URL = "http://localhost:11434/api/generate"
DOCS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../docs/hub"))

class ChatRequest(BaseModel):
    message: str
    context: str = ""

@app.post("/api/chat")
async def chat_endpoint(request: ChatRequest):
    try:
        workspace_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../"))
        norms_path = os.path.join(workspace_path, "cemig/normas")
        
        # Modo Bibliotecário: Busca trechos relevantes nos arquivos .md
        context_excerpts = []
        msg_lower = request.message.lower()
        
        # Mapeamento de arquivos por palavra-chave
        files_to_check = []
        if "individual" in msg_lower or "nd 5.1" in msg_lower or "nd5.1" in msg_lower:
            files_to_check.append("nd5_1_texto.md")
        if "coletivo" in msg_lower or "nd 5.2" in msg_lower or "nd5.2" in msg_lower:
            files_to_check.append("nd5_2_000001p.docx_texto.md")
        
        # Se o usuário perguntar por algo específico (ex: 63A), buscar em todos os arquivos principais
        keywords = ["63a", "63", "disjuntor", "cabo", "bitola", "aterramento", "material"]
        # Extrai números da mensagem para buscar também
        import re
        numbers = re.findall(r'\d+', msg_lower)
        
        found_keywords = [k for k in keywords if k in msg_lower] + numbers
        if found_keywords and not files_to_check:
            files_to_check = ["nd5_1_texto.md", "nd5_2_000001p.docx_texto.md"]

        for filename in files_to_check:
            path = os.path.join(norms_path, filename)
            if os.path.exists(path):
                with open(path, "r") as f:
                    lines = f.readlines()
                    for i, line in enumerate(lines):
                        line_lower = line.lower()
                        # Busca por palavras ou números isolados
                        if any(re.search(rf'\b{re.escape(k)}\b', line_lower) for k in found_keywords):
                            start = max(0, i - 3)
                            end = min(len(lines), i + 6)
                            context_excerpts.append(f"--- Trecho de {filename} (Linha {i}) ---\n" + "".join(lines[start:end]))
                        if len(context_excerpts) > 15: break 

        local_context = "\n".join(context_excerpts) if context_excerpts else "Ambiente local detectado. O usuário está perguntando sobre normas técnicas."

        async with httpx.AsyncClient(timeout=60.0) as client:
            payload = {
                "model": "llama3.1:8b",
                "prompt": f"Contexto das Normas CEMIG:\n{local_context}\n\nInstrução: Você é o Antigravity. Use o contexto acima para responder ao usuário. Se não encontrar a informação exata, diga o que encontrou de mais próximo.\n\nUsuário: {request.message}",
                "stream": False
            }
            response = await client.post(OLLAMA_URL, json=payload)
            response.raise_for_status()
            res_json = response.json()
            res_json["response"] = f"[Ollama] {res_json['response']}"
            res_json["debug_context"] = local_context # Enviando o contexto para a interface
            return res_json
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# Servir os arquivos estáticos do Docs Hub
app.mount("/", StaticFiles(directory=DOCS_DIR, html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
