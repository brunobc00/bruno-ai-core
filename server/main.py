from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from collections import defaultdict, OrderedDict
from datetime import date, timedelta
from pathlib import Path
import subprocess
import httpx
import os
import re
import uuid
import sys
import tempfile
import urllib.parse

from dotenv import load_dotenv
load_dotenv("/home/bruno/Documentos/Github/.env")

app = FastAPI(title="Antigravity Portal")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

OLLAMA_URL   = "http://localhost:11434/api/generate"
DOCS_DIR     = os.path.abspath(os.path.join(os.path.dirname(__file__), "../docs/hub"))
WORKSPACE    = Path("/home/bruno/Documentos/Github")
REDMINE_URL  = os.getenv("REDMINE_URL")
REDMINE_KEY  = os.getenv("REDMINE_API_KEY")

GOOGLE_CLIENT_ID     = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
GOOGLE_REDIRECT_URI  = "https://ia.automacaobbc.com/api/auth/google/callback"

_sessions: dict = {}   # session_id → {email, name, access_token, refresh_token}
_pdf_cache: dict = {}  # download_token → pdf_path


def _get_session(request: Request) -> dict | None:
    sid = request.cookies.get("session_id")
    return _sessions.get(sid) if sid else None


# ── Activity Report ───────────────────────────────────────────────────────────


def _parse_commits(raw: str) -> list[dict]:
    commits = []
    for line in raw.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("|", 3)
        if len(parts) == 4:
            commits.append({"hash": parts[0], "date": parts[1],
                            "author": parts[2], "msg": parts[3]})
    return commits


def _main_branch(repo_path: str) -> str:
    for candidate in ("main", "master"):
        r = subprocess.run(
            ["git", "-C", repo_path, "rev-parse", "--verify", f"origin/{candidate}"],
            capture_output=True
        )
        if r.returncode == 0:
            return candidate
    return "main"


def _git_report_until(since: str, until: str) -> dict:
    repos = {}
    for d in sorted(WORKSPACE.iterdir()):
        if not (d / ".git").exists():
            continue
        main = _main_branch(str(d))
        result = subprocess.run(
            ["git", "-C", str(d), "log", f"origin/{main}",
             f"--after={since} 00:00", f"--before={until} 23:59",
             "--format=%h|%ad|%an|%s", "--date=short"],
            capture_output=True, text=True
        )
        commits = _parse_commits(result.stdout)
        if commits:
            repos[d.name] = commits
    return repos


def _git_pending(since: str, until: str) -> dict:
    """Commits em branches remotas não mergeadas no main, agrupados por repo→branch."""
    result: dict = {}
    for d in sorted(WORKSPACE.iterdir()):
        if not (d / ".git").exists():
            continue
        main = _main_branch(str(d))

        branches_r = subprocess.run(
            ["git", "-C", str(d), "branch", "-r",
             "--no-merged", f"origin/{main}"],
            capture_output=True, text=True
        )
        branches = [
            b.strip() for b in branches_r.stdout.splitlines()
            if b.strip() and "HEAD" not in b
        ]

        repo_pending: dict = {}
        for branch in branches:
            log_r = subprocess.run(
                ["git", "-C", str(d), "log", branch,
                 f"--not", f"origin/{main}",
                 f"--after={since} 00:00", f"--before={until} 23:59",
                 "--format=%h|%ad|%an|%s", "--date=short"],
                capture_output=True, text=True
            )
            commits = _parse_commits(log_r.stdout)
            if commits:
                short_branch = branch.removeprefix("origin/")
                repo_pending[short_branch] = commits

        if repo_pending:
            result[d.name] = repo_pending
    return result


def _redmine_report(since: str, until: str) -> dict:
    until_api = (date.fromisoformat(until) + timedelta(days=1)).isoformat()
    url = (f"{REDMINE_URL}/issues.json"
           f"?updated_on=><{since}|{until_api}&limit=100&status_id=*")
    resp = httpx.get(url, headers={"X-Redmine-API-Key": REDMINE_KEY}, timeout=15)
    resp.raise_for_status()
    issues = resp.json().get("issues", [])

    by_person  = defaultdict(list)
    by_status  = defaultdict(int)
    resolved   = 0

    for i in issues:
        person = i.get("assigned_to", {}).get("name", "Sem responsável")
        status = i["status"]["name"]

        by_person[person].append({
            "id":      i["id"],
            "subject": i["subject"],
            "status":  status,
            "project": i.get("project", {}).get("name", "?"),
        })
        by_status[status] += 1
        if status in ("Resolved", "Closed"):
            resolved += 1

    return {
        "total":     len(issues),
        "by_person": dict(by_person),
        "by_status": dict(by_status),
        "resolved":  resolved,
    }


@app.get("/api/activity-report")
async def activity_report(
    since: str = Query(default=None),
    until: str = Query(default=None),
):
    today = date.today().isoformat()
    if not since:
        since = (date.today() - timedelta(days=1)).isoformat()
    if not until:
        until = today

    try:
        git_data     = _git_report_until(since, until)
        pending_data = _git_pending(since, until)
        redmine_data = _redmine_report(since, until)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    # Commits mergeados por autor
    git_by_author: dict = defaultdict(list)
    for repo, commits in git_data.items():
        for c in commits:
            git_by_author[c["author"]].append({**c, "repo": repo})

    # Commits pendentes por autor → {author: [{repo, branch, hash, date, msg}]}
    pending_by_author: dict = defaultdict(list)
    total_pending = 0
    for repo, branches in pending_data.items():
        for branch, commits in branches.items():
            for c in commits:
                pending_by_author[c["author"]].append(
                    {**c, "repo": repo, "branch": branch}
                )
                total_pending += 1

    redmine_people = {p for p in redmine_data["by_person"] if p != "Sem responsável"}
    all_people = sorted(
        redmine_people | set(git_by_author.keys()) | set(pending_by_author.keys())
    )

    people = []
    for person in all_people:
        redmine_tasks    = redmine_data["by_person"].get(person, [])
        git_commits      = list(git_by_author.get(person, []))
        pending_commits  = list(pending_by_author.get(person, []))
        if not redmine_tasks and not git_commits and not pending_commits:
            continue

        by_repo: dict = defaultdict(list)
        for c in git_commits:
            by_repo[c["repo"]].append(c)

        pending_by_branch: dict = defaultdict(list)
        for c in pending_commits:
            pending_by_branch[f"{c['repo']} / {c['branch']}"].append(c)

        people.append({
            "name":             person,
            "redmine_tasks":    redmine_tasks,
            "git_by_repo":      dict(by_repo),
            "total_commits":    len(git_commits),
            "pending_by_branch": dict(pending_by_branch),
            "total_pending":    len(pending_commits),
        })

    unassigned = [t for t in redmine_data["by_person"].get("Sem responsável", [])
                  if t["status"] == "New"]

    return {
        "since":          since,
        "today":          today,
        "total_commits":  sum(len(c) for c in git_data.values()),
        "active_repos":   len(git_data),
        "total_issues":   redmine_data["total"],
        "resolved":       redmine_data["resolved"],
        "total_pending":  total_pending,
        "people":         people,
        "unassigned":     unassigned,
    }


@app.get("/api/users")
async def list_users():
    """Retorna todos os autores Git (histórico completo) + assignees do Redmine."""
    git_authors: set[str] = set()
    for d in sorted(WORKSPACE.iterdir()):
        if not (d / ".git").exists():
            continue
        result = subprocess.run(
            ["git", "-C", str(d), "log", "--format=%an"],
            capture_output=True, text=True
        )
        for name in result.stdout.strip().splitlines():
            name = name.strip()
            if name:
                git_authors.add(name)

    redmine_authors: set[str] = set()
    try:
        url = f"{REDMINE_URL}/issues.json?limit=100&status_id=*&assigned_to_id=*"
        resp = httpx.get(url, headers={"X-Redmine-API-Key": REDMINE_KEY}, timeout=15)
        if resp.is_success:
            for i in resp.json().get("issues", []):
                name = i.get("assigned_to", {}).get("name", "")
                if name and name != "Sem responsável":
                    redmine_authors.add(name)
    except Exception:
        pass

    all_users = sorted(git_authors | redmine_authors)
    return {"users": all_users}


# ── Chat ──────────────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str
    context: str = ""

@app.post("/api/chat")
async def chat_endpoint(request: ChatRequest):
    try:
        workspace_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../"))
        norms_path = os.path.join(workspace_path, "cemig/normas")

        context_excerpts = []
        msg_lower = request.message.lower()

        files_to_check = []
        if "individual" in msg_lower or "nd 5.1" in msg_lower or "nd5.1" in msg_lower:
            files_to_check.append("nd5_1_texto.md")
        if "coletivo" in msg_lower or "nd 5.2" in msg_lower or "nd5.2" in msg_lower:
            files_to_check.append("nd5_2_000001p.docx_texto.md")

        keywords = ["63a", "63", "disjuntor", "cabo", "bitola", "aterramento", "material"]
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
                        if any(re.search(rf'\b{re.escape(k)}\b', line_lower) for k in found_keywords):
                            start = max(0, i - 3)
                            end = min(len(lines), i + 6)
                            context_excerpts.append(f"--- Trecho de {filename} (Linha {i}) ---\n" + "".join(lines[start:end]))
                        if len(context_excerpts) > 15:
                            break

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
            res_json["debug_context"] = local_context
            return res_json
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Google OAuth ─────────────────────────────────────────────────────────────

@app.get("/api/auth/google")
async def auth_google():
    params = urllib.parse.urlencode({
        "client_id":     GOOGLE_CLIENT_ID,
        "redirect_uri":  GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope":         "openid email profile https://www.googleapis.com/auth/spreadsheets.readonly",
        "access_type":   "offline",
        "prompt":        "consent",
    })
    return RedirectResponse(f"https://accounts.google.com/o/oauth2/auth?{params}")


@app.get("/api/auth/google/callback")
async def auth_google_callback(code: str):
    async with httpx.AsyncClient(timeout=15) as client:
        token_r = await client.post("https://oauth2.googleapis.com/token", data={
            "code":          code,
            "client_id":     GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri":  GOOGLE_REDIRECT_URI,
            "grant_type":    "authorization_code",
        })
        token_r.raise_for_status()
        tokens = token_r.json()

        user_r = await client.get(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
        )
        user = user_r.json()

    sid = str(uuid.uuid4())
    _sessions[sid] = {
        "email":         user.get("email"),
        "name":          user.get("name"),
        "access_token":  tokens.get("access_token"),
        "refresh_token": tokens.get("refresh_token"),
    }
    resp = RedirectResponse(url="/#orcamentos")
    resp.set_cookie("session_id", sid, httponly=True, samesite="lax", max_age=86400 * 7)
    return resp


@app.get("/api/auth/me")
async def auth_me(request: Request):
    session = _get_session(request)
    if not session:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return {"email": session["email"], "name": session["name"]}


@app.get("/api/auth/logout")
async def auth_logout(request: Request):
    sid = request.cookies.get("session_id")
    if sid:
        _sessions.pop(sid, None)
    resp = RedirectResponse(url="/")
    resp.delete_cookie("session_id")
    return resp


# ── Google Sheets → Orçamento ────────────────────────────────────────────────

class SheetsRequest(BaseModel):
    url: str


def _parse_brl(s: str) -> float:
    s = s.replace("R$", "").replace(".", "").replace(",", ".").strip()
    try:
        return float(s)
    except ValueError:
        return 0.0


def _fmt_brl(v: float) -> str:
    return f"R$ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


@app.post("/api/sheets/processar")
async def sheets_processar(body: SheetsRequest, request: Request):
    session = _get_session(request)
    if not session:
        raise HTTPException(status_code=401, detail="Not authenticated")

    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", body.url)
    if not m:
        raise HTTPException(status_code=400, detail="URL inválida")
    sid = m.group(1)

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"https://sheets.googleapis.com/v4/spreadsheets/{sid}/values/A:AJ",
            headers={"Authorization": f"Bearer {session['access_token']}"},
        )
        if resp.status_code == 401:
            raise HTTPException(status_code=401, detail="Token expirado. Faça login novamente.")
        resp.raise_for_status()

    rows = resp.json().get("values", [])
    if len(rows) < 2:
        raise HTTPException(status_code=400, detail="Planilha vazia")

    headers = rows[0]

    def find_col(candidates: list[str], default: int) -> int:
        for name in candidates:
            for i, h in enumerate(headers):
                if h.strip().lower() == name.lower():
                    return i
        return default

    col_amb   = find_col(["AMBIENTE", "ambiente"], 2)
    col_grp   = find_col(["GRUPO", "grupo"], 8)
    col_qtd   = find_col(["Quantidade", "QTD", "quantidade"], 5)
    col_vunit = find_col(["valor unit c desconto", "valor unitario c desc", "valor unit. c/desc"], 19)
    col_vtot  = find_col(["valor total c/desc", "valor total c desc"], 20)

    # Payment columns: ENT(29), 30d(30), 60d(31), 90d(32), 120d(33), 150d(34), 180d(35), 210d(36)
    PAYMENT_LABELS = ["À Vista", "30 Dias", "60 Dias", "90 Dias", "120 Dias", "150 Dias", "180 Dias", "210 Dias"]
    PAYMENT_COLS   = [29, 30, 31, 32, 33, 34, 35, 36]
    payment_sums   = [0.0] * len(PAYMENT_COLS)

    pivot: dict = OrderedDict()
    for row in rows[1:]:
        def cell(i: int) -> str:
            return row[i].strip() if i < len(row) else ""

        ambiente = cell(col_amb)
        grupo    = cell(col_grp)
        if not ambiente or not grupo:
            continue

        try:
            qtd = int(cell(col_qtd).replace(",", "").split(".")[0])
        except (ValueError, IndexError):
            qtd = 0
        if qtd <= 0:
            continue

        vunit = _parse_brl(cell(col_vunit))
        vtot  = _parse_brl(cell(col_vtot))

        key = (ambiente, grupo)
        if key not in pivot:
            pivot[key] = {"ambiente": ambiente, "grupo": grupo, "qtd": 0, "vunit": vunit, "vtotal": 0.0}
        pivot[key]["qtd"]    += qtd
        pivot[key]["vtotal"] += vtot

        for i, col_idx in enumerate(PAYMENT_COLS):
            payment_sums[i] += _parse_brl(cell(col_idx))

    items = list(pivot.values())
    total = sum(i["vtotal"] for i in items)

    pagamento = [
        {"prazo": label, "valor": round(v, 2)}
        for label, v in zip(PAYMENT_LABELS, payment_sums)
        if v > 0
    ]

    return {"items": items, "total": total, "pagamento": pagamento}


_NOTAS_TECNICAS = """\
## 🔍 Notas Técnicas e Observações

1.  **Tecnologia de LED Integrado e Manutenção:** Todos os spots, balizadores e embutidos de solo especificados utilizam tecnologia de **LED Integrado**. Nestes itens, o **driver (fonte) não é integrado**, o que garante extrema facilidade em eventuais manutenções futuras, permitindo a substituição apenas do driver sem necessidade de trocar a peça inteira.
2.  **Conformidade NBR 5410 (Segurança):** Para as áreas molhadas, o sistema utiliza alimentação de **Extra Baixa Tensão**, prevenindo riscos de choque elétrico conforme exigência de segurança.
3.  **Sistemas Completos:** O orçamento contempla o fornecimento de todos os drivers e fontes de alimentação necessários para o funcionamento dos perfis de LED e sistemas de extra baixa tensão.
4.  **Adequação de Fachos:** Todos os spots fornecidos terão os fachos de luz adequados ao seu ambiente específico, garantindo conforto visual e o efeito arquitetônico planejado.
5.  **Garantia de Fábrica:**
    *   **5 Anos de Garantia:** Toda a linha de Jardim (balizadores, espetos, embutidos de solo) e todos os Spots de LED.
    *   **2 Anos de Garantia:** Linha de Arandelas e Lâmpadas LED.
6.  **Conformidade com Projeto:** Todos os itens foram selecionados para estarem em conformidade com o projeto apresentado.
"""


class OrcamentoRequest(BaseModel):
    items: list[dict]
    total: float
    client_name: str
    client_cnpj: str = ""
    validade: str = ""       # ex: "28/04/2026"
    observacoes: str = ""    # texto livre para a seção Descrição do Projeto
    pagamento: list[dict] = []  # [{prazo, valor}]


@app.post("/api/orcamento/gerar-pdf")
async def gerar_pdf_api(body: OrcamentoRequest, request: Request):
    session = _get_session(request)
    if not session:
        raise HTTPException(status_code=401, detail="Not authenticated")

    from datetime import date as _date
    today = _date.today().strftime("%d/%m/%Y")

    table_rows = ""
    for item in body.items:
        table_rows += (
            f"| {item['ambiente']} | {item['grupo']} | {item['qtd']} "
            f"| {_fmt_brl(item['vunit'])} | {_fmt_brl(item['vtotal'])} |\n"
        )
    table_rows += f"| **TOTAL GERAL** | | | | **{_fmt_brl(body.total)}** |\n"

    # Payment conditions section
    pagamento_md = ""
    if body.pagamento:
        parcela_num = ["Entrada"] + [f"{i}ª Parcela" for i in range(1, len(body.pagamento))]
        rows_pag = ""
        for label, p in zip(parcela_num, body.pagamento):
            rows_pag += f"| {label} | {p['prazo']} | {_fmt_brl(p['valor'])} |\n"
        pagamento_md = f"""\
## 💳 Condições de Pagamento

| Parcela | Prazo | Valor |
| :--- | :--- | :--- |
{rows_pag}
---

"""

    # Optional project description
    descricao_md = ""
    if body.observacoes.strip():
        descricao_md = f"""\
## 📝 Descrição do Projeto

{body.observacoes.strip()}

---

"""

    validade_line = f"**VALIDADE:** {body.validade}\n" if body.validade else ""

    md_content = f"""# ORÇAMENTO DE ILUMINAÇÃO - {body.client_name.upper()}

**CLIENTE:** {body.client_name.upper()}
**CNPJ:** {body.client_cnpj}
**DATA:** {today}
{validade_line}
---

{descricao_md}## 📦 Itens do Orçamento

| Ambiente | Grupo | Qtd | Valor Unit. | Total |
| :--- | :--- | :---: | :--- | :--- |
{table_rows}
---

{pagamento_md}{_NOTAS_TECNICAS}

---

## 🖋️ Confirmação de Pedido
Confirmo os valores, condições de pagamentos e quantidades dos produtos acima relacionados.
"""

    tmp_dir = Path(tempfile.mkdtemp(prefix="orcamento_"))
    md_path = tmp_dir / "orcamento.md"
    md_path.write_text(md_content, encoding="utf-8")

    script  = WORKSPACE / "carvalhaescomercial-orcamentos" / "scripts" / "gerar_orcamento.py"
    py_bin  = Path(sys.executable)

    result = subprocess.run(
        [str(py_bin), str(script), str(md_path)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise HTTPException(status_code=500, detail=f"Erro ao gerar PDF: {result.stderr}")

    pdf_path = tmp_dir / "orcamento.pdf"
    if not pdf_path.exists():
        raise HTTPException(status_code=500, detail="PDF não foi gerado")

    token = str(uuid.uuid4())
    _pdf_cache[token] = str(pdf_path)

    safe_name = re.sub(r"[^a-z0-9-]", "-", body.client_name.lower())
    return {"token": token, "filename": f"orcamento-{safe_name}.pdf"}


@app.get("/api/orcamento/download/{token}")
async def download_pdf(token: str):
    pdf_path = _pdf_cache.get(token)
    if not pdf_path or not Path(pdf_path).exists():
        raise HTTPException(status_code=404, detail="PDF não encontrado ou expirado")
    return FileResponse(pdf_path, media_type="application/pdf", filename="orcamento.pdf")


# ── Static (must be last) ─────────────────────────────────────────────────────

# Serve logo assets de projetos fora do docs/hub
_LOGO_DIRS = {
    "br.com.paconstrushop.services.contracts": WORKSPACE / "br.com.paconstrushop.services.contracts" / "assets",
    "carvalhaescomercial-orcamentos":           WORKSPACE / "carvalhaescomercial-orcamentos" / "assets",
}
for _route, _dir in _LOGO_DIRS.items():
    if _dir.exists():
        app.mount(f"/{_route}/assets", StaticFiles(directory=str(_dir)), name=_route)

app.mount("/", StaticFiles(directory=DOCS_DIR, html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
