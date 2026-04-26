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
import json
import os
import re
import uuid
import sys
import tempfile
import urllib.parse
from datetime import datetime, timezone

import jwt as pyjwt

from dotenv import load_dotenv
load_dotenv("/home/bruno/Documentos/Github/.env")

import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from db import init_db
from fornecedores import router as fornecedores_router, _global_router as fornecedores_global_router

app = FastAPI(title="Antigravity Portal")

@app.on_event("startup")
def startup():
    init_db()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(fornecedores_router)
app.include_router(fornecedores_global_router)

OLLAMA_URL   = "http://localhost:11434/api/generate"
DOCS_DIR     = os.path.abspath(os.path.join(os.path.dirname(__file__), "../docs/hub"))
WORKSPACE    = Path("/home/bruno/Documentos/Github")
REDMINE_URL  = os.getenv("REDMINE_URL")
REDMINE_KEY  = os.getenv("REDMINE_API_KEY")

GOOGLE_CLIENT_ID     = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
GOOGLE_REDIRECT_URI  = "https://ia.automacaobbc.com/api/auth/google/callback"
JWT_SECRET           = os.getenv("JWT_SECRET", "fallback-insecure-secret-change-me")
JWT_ALGORITHM        = "HS256"
JWT_EXPIRE_DAYS      = 7

_pdf_cache: dict = {}  # download_token → pdf_path


def _get_session(request: Request) -> dict | None:
    token = request.cookies.get("session_id")
    if not token:
        return None
    try:
        return pyjwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except pyjwt.PyJWTError:
        return None


def _make_jwt(email: str, name: str, access_token: str, refresh_token: str | None) -> str:
    payload = {
        "email":         email,
        "name":          name,
        "access_token":  access_token,
        "refresh_token": refresh_token,
        "exp":           datetime.now(timezone.utc) + timedelta(days=JWT_EXPIRE_DAYS),
    }
    return pyjwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


# ── Activity Report ───────────────────────────────────────────────────────────


def _parse_commits(raw: str) -> list[dict]:
    commits = []
    for line in raw.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("|", 4)
        if len(parts) == 5:
            commits.append({"hash": parts[0], "date": parts[1],
                            "author": parts[2], "email": parts[3], "msg": parts[4]})
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
             "--format=%h|%ad|%an|%ae|%s", "--date=short"],
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
                 "--format=%h|%ad|%an|%ae|%s", "--date=short"],
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
            "date":    i.get("updated_on", "")[:10],
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


def _get_redmine_active_users() -> list[dict]:
    """Retorna usuários ativos do Redmine com nome e email."""
    try:
        url = f"{REDMINE_URL}/users.json?limit=100&status=1"
        resp = httpx.get(url, headers={"X-Redmine-API-Key": REDMINE_KEY}, timeout=10)
        if resp.is_success:
            return [
                {
                    "name":  f"{u.get('firstname','')} {u.get('lastname','')}".strip(),
                    "email": u.get("mail", "").lower(),
                }
                for u in resp.json().get("users", [])
                if u.get("mail")
            ]
    except Exception:
        pass
    return []


def _fmt_date_pt(iso: str) -> str:
    months = ["janeiro","fevereiro","março","abril","maio","junho",
              "julho","agosto","setembro","outubro","novembro","dezembro"]
    try:
        d = date.fromisoformat(iso)
        return f"{d.day} de {months[d.month-1]} de {d.year}"
    except Exception:
        return iso


def _build_report_html(since: str, until: str, people: list, person_filter: str) -> str:
    CSS = """
    body { font-family: Arial, sans-serif; font-size: 11px; color: #1a1a1a; margin: 0; padding: 20px; }
    h1 { font-size: 18px; color: #0d4f8b; margin-bottom: 4px; }
    .subtitle { color: #555; font-size: 11px; margin-bottom: 24px; }
    .person-block { margin-bottom: 32px; page-break-inside: avoid; }
    .person-name { font-size: 15px; font-weight: bold; color: #0d4f8b; margin-bottom: 2px; }
    .person-email { font-size: 10px; color: #666; margin-bottom: 10px; }
    .no-activity { background: #fff8e1; border-left: 4px solid #f0a500; padding: 10px 14px;
                   border-radius: 4px; color: #7a5700; font-size: 11px; margin-top: 6px; }
    .day-block { margin-bottom: 16px; }
    .day-header { font-size: 12px; font-weight: bold; color: #444; background: #f0f4f8;
                  padding: 4px 10px; border-radius: 4px; margin-bottom: 6px; }
    .section-label { font-size: 10px; font-weight: bold; color: #888; text-transform: uppercase;
                     letter-spacing: .5px; margin: 6px 0 3px; }
    table { width: 100%; border-collapse: collapse; margin-bottom: 4px; }
    th { background: #e8edf2; text-align: left; padding: 4px 8px; font-size: 10px; }
    td { padding: 3px 8px; border-bottom: 1px solid #eee; font-size: 10px; vertical-align: top; }
    .tag-rm { display: inline-block; background: #dbeafe; color: #1d4ed8; border-radius: 3px;
              padding: 1px 5px; font-size: 9px; }
    .tag-git { display: inline-block; background: #dcfce7; color: #15803d; border-radius: 3px;
               padding: 1px 5px; font-size: 9px; }
    footer { margin-top: 40px; font-size: 9px; color: #aaa; border-top: 1px solid #eee; padding-top: 8px; }
    """
    per_label = f"{_fmt_date_pt(since)}" if since == until else f"{_fmt_date_pt(since)} a {_fmt_date_pt(until)}"
    body = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
    <style>{CSS}</style></head><body>
    <h1>Relatório de Atividades</h1>
    <div class="subtitle">Período: {per_label} &nbsp;|&nbsp; Gerado em {_fmt_date_pt(date.today().isoformat())}</div>
    """

    for p in people:
        email_str = f" &lt;{p['email']}&gt;" if p.get("email") else ""
        body += f'<div class="person-block">'
        body += f'<div class="person-name">{p["name"]}</div>'
        body += f'<div class="person-email">{p.get("email", "e-mail não disponível")}</div>'

        if p.get("no_activity"):
            msg = (f'O usuário <strong>{p["name"]}</strong>{email_str} não realizou '
                   f'nenhuma atividade nesse período.')
            body += f'<div class="no-activity">{msg}</div>'
        else:
            days = p.get("days", {})
            for day_iso in sorted(days.keys()):
                day_data = days[day_iso]
                body += f'<div class="day-block"><div class="day-header">📅 {_fmt_date_pt(day_iso)}</div>'

                rm_tasks = day_data.get("redmine_tasks", [])
                if rm_tasks:
                    body += '<div class="section-label">Redmine</div>'
                    body += '<table><tr><th>#</th><th>Projeto</th><th>Tarefa</th><th>Status</th></tr>'
                    for t in rm_tasks:
                        body += f'<tr><td>#{t["id"]}</td><td>{t["project"]}</td><td>{t["subject"]}</td><td>{t["status"]}</td></tr>'
                    body += '</table>'

                git_commits = day_data.get("git_commits", [])
                if git_commits:
                    body += '<div class="section-label">Git</div>'
                    body += '<table><tr><th>Hash</th><th>Repositório</th><th>Commit</th></tr>'
                    for c in git_commits:
                        body += f'<tr><td>{c["hash"]}</td><td>{c["repo"]}</td><td>{c["msg"]}</td></tr>'
                    body += '</table>'

                body += '</div>'

        body += '</div>'

    body += f'<footer>Antigravity Hub — ia.automacaobbc.com</footer></body></html>'
    return body


@app.get("/api/activity-report/pdf")
async def activity_report_pdf(
    since: str = Query(default=None),
    until: str = Query(default=None),
    person: str = Query(default=None),
):
    from fastapi.responses import Response
    today = date.today().isoformat()
    if not since:
        since = (date.today() - timedelta(days=1)).isoformat()
    if not until:
        until = today

    try:
        git_data     = _git_report_until(since, until)
        redmine_data = _redmine_report(since, until)
        active_users = _get_redmine_active_users()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    email_to_name = {u["email"]: u["name"] for u in active_users if u["email"]}
    name_to_email = {u["name"]: u["email"] for u in active_users}
    active_names  = {u["name"] for u in active_users}

    git_by_person: dict = defaultdict(list)
    for repo, commits in git_data.items():
        for c in commits:
            name = email_to_name.get(c["email"].lower(), "")
            if name:
                git_by_person[name].append({**c, "repo": repo})

    redmine_people = {p for p in redmine_data["by_person"]
                      if p != "Sem responsável" and p in active_names}
    all_people = sorted(redmine_people | set(git_by_person.keys()))

    if person:
        matched = [p for p in all_people if p == person]
        if not matched:
            people_data = [{"name": person, "email": name_to_email.get(person, ""), "no_activity": True}]
        else:
            all_people = matched
            people_data = []
    else:
        people_data = []

    for p in all_people:
        rm_tasks    = redmine_data["by_person"].get(p, [])
        git_commits = list(git_by_person.get(p, []))

        days: dict = defaultdict(lambda: {"redmine_tasks": [], "git_commits": []})
        for t in rm_tasks:
            days[t.get("date", since)]["redmine_tasks"].append(t)
        for c in git_commits:
            days[c["date"]]["git_commits"].append(c)

        people_data.append({
            "name":        p,
            "email":       name_to_email.get(p, ""),
            "no_activity": False,
            "days":        dict(sorted(days.items())),
        })

    html = _build_report_html(since, until, people_data, person or "")
    try:
        from weasyprint import HTML as WP
        pdf_bytes = WP(string=html).write_pdf()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao gerar PDF: {e}")

    filename = f"relatorio_{since}_{until}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


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
        git_data       = _git_report_until(since, until)
        pending_data   = _git_pending(since, until)
        redmine_data   = _redmine_report(since, until)
        active_users   = _get_redmine_active_users()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    # Mapa email → nome canônico (Redmine)
    email_to_name = {u["email"]: u["name"] for u in active_users if u["email"]}
    active_names  = {u["name"] for u in active_users}

    # Commits mergeados por nome canônico (via email)
    git_by_person: dict = defaultdict(list)
    for repo, commits in git_data.items():
        for c in commits:
            name = email_to_name.get(c["email"].lower(), "")
            if name:
                git_by_person[name].append({**c, "repo": repo})

    # Commits pendentes por nome canônico
    pending_by_person: dict = defaultdict(list)
    total_pending = 0
    for repo, branches in pending_data.items():
        for branch, commits in branches.items():
            for c in commits:
                name = email_to_name.get(c["email"].lower(), "")
                if name:
                    pending_by_person[name].append({**c, "repo": repo, "branch": branch})
                    total_pending += 1

    # Só pessoas ativas no Redmine com atividade no período
    redmine_people = {p for p in redmine_data["by_person"]
                      if p != "Sem responsável" and p in active_names}
    all_people = sorted(
        redmine_people | set(git_by_person.keys()) | set(pending_by_person.keys())
    )

    people = []
    for person in all_people:
        redmine_tasks   = redmine_data["by_person"].get(person, [])
        git_commits     = list(git_by_person.get(person, []))
        pending_commits = list(pending_by_person.get(person, []))
        if not redmine_tasks and not git_commits and not pending_commits:
            continue

        by_repo: dict = defaultdict(list)
        for c in git_commits:
            by_repo[c["repo"]].append(c)

        pending_by_branch: dict = defaultdict(list)
        for c in pending_commits:
            pending_by_branch[f"{c['repo']} / {c['branch']}"].append(c)

        people.append({
            "name":              person,
            "redmine_tasks":     redmine_tasks,
            "git_by_repo":       dict(by_repo),
            "total_commits":     len(git_commits),
            "pending_by_branch": dict(pending_by_branch),
            "total_pending":     len(pending_commits),
        })

    unassigned = [t for t in redmine_data["by_person"].get("Sem responsável", [])
                  if t["status"] == "New"]

    return {
        "since":         since,
        "today":         today,
        "total_commits": sum(len(c) for c in git_data.values()),
        "active_repos":  len(git_data),
        "total_issues":  redmine_data["total"],
        "resolved":      redmine_data["resolved"],
        "total_pending": total_pending,
        "people":        people,
        "unassigned":    unassigned,
    }


@app.get("/api/users")
async def list_users():
    """Retorna apenas usuários ativos do Redmine."""
    users = _get_redmine_active_users()
    return {"users": sorted(u["name"] for u in users)}


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
        "scope":         "openid email profile https://www.googleapis.com/auth/spreadsheets",
        "access_type":   "offline",
        "prompt":        "select_account",
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

    token = _make_jwt(
        email=user.get("email"),
        name=user.get("name"),
        access_token=tokens.get("access_token"),
        refresh_token=tokens.get("refresh_token"),
    )
    resp = RedirectResponse(url="/#orcamentos")
    resp.set_cookie("session_id", token, httponly=True, samesite="lax", max_age=86400 * JWT_EXPIRE_DAYS)
    return resp


@app.get("/api/auth/me")
async def auth_me(request: Request):
    session = _get_session(request)
    if not session:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return {"email": session["email"], "name": session["name"]}


@app.get("/api/auth/logout")
async def auth_logout():
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


def _col_letter(n: int) -> str:
    """Converte número de coluna (1-based) em letra(s): 1→A, 26→Z, 27→AA."""
    result = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        result = chr(65 + r) + result
    return result


def _parse_sheet_data(rows: list, headers_row: list) -> dict:
    """Processa linhas brutas da Sheets API e retorna items/total/pagamento."""
    def find_col(candidates: list[str], default: int) -> int:
        for name in candidates:
            for i, h in enumerate(headers_row):
                if h.strip().lower() == name.lower():
                    return i
        return default

    col_amb   = find_col(["AMBIENTE", "ambiente"], 2)
    col_grp   = find_col(["GRUPO", "grupo"], 8)
    col_qtd   = find_col(["Quantidade", "QTD", "quantidade"], 5)
    col_vunit = find_col(["valor unit c desconto", "valor unitario c desc", "valor unit. c/desc",
                           "Valor Unitário", "valor unitario", "vunit"], 19)
    col_vtot  = find_col(["valor total c/desc", "valor total c desc",
                           "Valor Total", "valor total", "vtotal"], 20)

    # Busca colunas de pagamento por nome, com fallback nos índices do formato antigo
    header_lower = {h.strip().lower(): i for i, h in enumerate(headers_row)}
    PAYMENT_LABELS   = ["À Vista", "28 Dias", "30 Dias", "60 Dias", "90 Dias", "120 Dias", "150 Dias", "180 Dias", "210 Dias"]
    PAYMENT_FALLBACK = {"À Vista": 29, "28 Dias": -1, "30 Dias": 30, "60 Dias": 31,
                        "90 Dias": 32, "120 Dias": 33, "150 Dias": 34, "180 Dias": 35, "210 Dias": 36}
    payment_cols = [header_lower.get(lbl.lower(), PAYMENT_FALLBACK.get(lbl, -1)) for lbl in PAYMENT_LABELS]
    payment_sums = [0.0] * len(PAYMENT_LABELS)

    pivot: dict = OrderedDict()
    for row in rows:
        get = lambda i, r=row: r[i].strip() if i < len(r) else ""
        ambiente = get(col_amb)
        grupo    = get(col_grp)
        if not ambiente or not grupo:
            continue
        try:
            qtd = int(get(col_qtd).replace(",", "").split(".")[0])
        except (ValueError, IndexError):
            qtd = 0
        if qtd <= 0:
            continue
        vunit = _parse_brl(get(col_vunit))
        vtot  = _parse_brl(get(col_vtot))
        key = (ambiente, grupo)
        if key not in pivot:
            pivot[key] = {"ambiente": ambiente, "grupo": grupo, "qtd": 0, "vunit": vunit, "vtotal": 0.0}
        pivot[key]["qtd"]    += qtd
        pivot[key]["vtotal"] += vtot
        for i, col_idx in enumerate(payment_cols):
            if col_idx >= 0:
                payment_sums[i] += _parse_brl(get(col_idx))

    items = list(pivot.values())
    total = sum(i["vtotal"] for i in items)
    pagamento = [
        {"prazo": label, "valor": round(v, 2)}
        for label, v in zip(PAYMENT_LABELS, payment_sums)
        if v > 0
    ]
    return {"items": items, "total": total, "pagamento": pagamento}


@app.post("/api/sheets/processar")
async def sheets_processar(body: SheetsRequest, request: Request):
    session = _get_session(request)
    if not session:
        raise HTTPException(status_code=401, detail="Not authenticated")

    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", body.url)
    if not m:
        raise HTTPException(status_code=400, detail="URL inválida")
    sid = m.group(1)

    # Extrai gid — pode estar em ?gid= ou no fragmento #gid=
    gid_m = re.search(r"gid=(\d+)", body.url)
    target_gid = int(gid_m.group(1)) if gid_m else 0

    async with httpx.AsyncClient(timeout=45) as client:
        # 1) Busca metadata: nome da aba e número real de colunas
        meta = await client.get(
            f"https://sheets.googleapis.com/v4/spreadsheets/{sid}?fields=sheets.properties",
            headers={"Authorization": f"Bearer {session['access_token']}"},
        )
        if meta.status_code == 401:
            raise HTTPException(status_code=401, detail="Token expirado. Faça login novamente.")
        meta.raise_for_status()

        sheet_title = None
        col_count   = 50  # fallback
        sheets_list = meta.json().get("sheets", [])
        for sh in sheets_list:
            props = sh.get("properties", {})
            if props.get("sheetId") == target_gid:
                sheet_title = props.get("title", "")
                col_count   = props.get("gridProperties", {}).get("columnCount", 50)
                break
        # Se o gid não foi encontrado, usa a primeira aba
        if sheet_title is None and sheets_list:
            props       = sheets_list[0].get("properties", {})
            sheet_title = props.get("title", "")
            col_count   = props.get("gridProperties", {}).get("columnCount", 50)

        # 2) Constrói o range dinâmico com todas as colunas reais da aba
        last_col    = _col_letter(col_count)
        sheet_range = f"'{sheet_title}'!A:{last_col}" if sheet_title else f"A:{last_col}"

        # 3) Busca os dados — range precisa de URL-encode (nomes de aba com espaços)
        encoded_range = urllib.parse.quote(sheet_range, safe="!:'")
        resp = await client.get(
            f"https://sheets.googleapis.com/v4/spreadsheets/{sid}/values/{encoded_range}",
            headers={"Authorization": f"Bearer {session['access_token']}"},
        )
        if resp.status_code == 401:
            raise HTTPException(status_code=401, detail="Token expirado. Faça login novamente.")
        resp.raise_for_status()

    rows = resp.json().get("values", [])
    if len(rows) < 2:
        raise HTTPException(
            status_code=400,
            detail=f"Aba '{sheet_title}' está vazia ou sem dados suficientes."
        )

    result = _parse_sheet_data(rows[1:], rows[0])
    if not result["items"]:
        header_sample = " | ".join(h.strip() for h in rows[0][:12] if h.strip()) or "(sem cabeçalho)"
        raise HTTPException(
            status_code=422,
            detail=f"Nenhum item encontrado na aba '{sheet_title}'. Cabeçalhos detectados: [{header_sample}]. Verifique se as colunas AMBIENTE e GRUPO existem e se há linhas com Quantidade > 0."
        )
    return result


class SheetsPasteRequest(BaseModel):
    texto: str


@app.post("/api/sheets/processar-texto")
async def sheets_processar_texto(body: SheetsPasteRequest, request: Request):
    session = _get_session(request)
    if not session:
        raise HTTPException(status_code=401, detail="Not authenticated")

    raw = body.texto.replace('\r\n', '\n').replace('\r', '\n').strip()
    lines = raw.splitlines()
    if len(lines) < 2:
        raise HTTPException(status_code=400, detail="Dados insuficientes — cole a planilha incluindo o cabeçalho.")

    rows = [line.split('\t') for line in lines]
    result = _parse_sheet_data(rows[1:], rows[0])
    if not result["items"]:
        raise HTTPException(
            status_code=422,
            detail="Nenhum item encontrado. Verifique se as colunas AMBIENTE e GRUPO estão presentes e se há linhas com Quantidade > 0."
        )
    return result


_TEMPLATE_HEADERS = ["AMBIENTE", "GRUPO", "Quantidade", "Valor Unitário", "Valor Total"]
_TEMPLATE_EXAMPLE = ["Sala", "Spot LED", "4", "300,00", "1.200,00"]


@app.post("/api/sheets/criar-template")
async def sheets_criar_template(body: SheetsRequest, request: Request):
    session = _get_session(request)
    if not session:
        raise HTTPException(status_code=401, detail="Not authenticated")

    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", body.url)
    if not m:
        raise HTTPException(status_code=400, detail="URL de planilha inválida.")
    sid = m.group(1)
    token = session["access_token"]

    async with httpx.AsyncClient(timeout=30) as client:
        # 1) Adiciona a nova aba
        add_r = await client.post(
            f"https://sheets.googleapis.com/v4/spreadsheets/{sid}:batchUpdate",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"requests": [{"addSheet": {"properties": {"title": "Orçamento — Template"}}}]},
        )
        if add_r.status_code == 401:
            raise HTTPException(status_code=401, detail="Token expirado. Faça login novamente.")
        if add_r.status_code == 400:
            detail = add_r.json().get("error", {}).get("message", "Erro ao criar aba.")
            raise HTTPException(status_code=400, detail=detail)
        add_r.raise_for_status()

        new_gid   = add_r.json()["replies"][0]["addSheet"]["properties"]["sheetId"]
        new_title = "Orçamento — Template"

        # 2) Escreve cabeçalho + linha exemplo
        encoded_range = urllib.parse.quote(f"'{new_title}'!A1", safe="!:'")
        write_r = await client.put(
            f"https://sheets.googleapis.com/v4/spreadsheets/{sid}/values/{encoded_range}"
            "?valueInputOption=USER_ENTERED",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"values": [_TEMPLATE_HEADERS, _TEMPLATE_EXAMPLE]},
        )
        write_r.raise_for_status()

    return {"gid": new_gid, "title": new_title}


OLLAMA_MODEL = "llama3.1:8b"
OLLAMA_BASE  = "http://localhost:11434"


@app.get("/api/ollama/status")
async def ollama_status():
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            ps = await client.get(f"{OLLAMA_BASE}/api/ps")
            ps.raise_for_status()
            modelos_ativos = [m["name"] for m in ps.json().get("models", [])]
            carregado = any(OLLAMA_MODEL in m for m in modelos_ativos)
            return {"online": True, "carregado": carregado, "model": OLLAMA_MODEL, "ativos": modelos_ativos}
    except Exception:
        return {"online": False, "carregado": False, "model": OLLAMA_MODEL, "ativos": []}


@app.post("/api/ollama/carregar")
async def ollama_carregar(request: Request):
    session = _get_session(request)
    if not session:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(
                f"{OLLAMA_BASE}/api/generate",
                json={"model": OLLAMA_MODEL, "prompt": "", "keep_alive": -1, "stream": False},
            )
            r.raise_for_status()
        return {"ok": True, "model": OLLAMA_MODEL}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Ollama não respondeu: {e}")


class MelhorarTextoRequest(BaseModel):
    texto: str
    modo: str = "completo"   # "minimo" | "completo" | "livre"
    instrucao: str = ""


def _build_melhorar_prompt(texto: str, modo: str, instrucao: str) -> str:
    if modo == "minimo":
        return (
            "Corrija erros de ortografia e gramática (português brasileiro) e formate em Markdown básico.\n"
            "Regras:\n"
            "- Use **negrito** apenas para termos técnicos e títulos\n"
            "- Corrija SOMENTE erros de escrita — não altere, resuma ou expanda o conteúdo\n"
            "- Retorne SOMENTE o texto corrigido, sem comentários ou prefixos\n\n"
            f"Texto:\n{texto}"
        )
    if modo == "livre":
        base = f"Texto base:\n{texto}" if texto else "(sem texto base — crie a partir da instrução)"
        return (
            "Você é um assistente de redação para orçamentos comerciais de iluminação.\n"
            f"Instrução: {instrucao}\n\n"
            "Regras:\n"
            "- Português brasileiro, tom profissional e formal\n"
            "- Formate em Markdown: **negrito** para termos técnicos, listas quando adequado\n"
            "- Preserve informações técnicas do texto base (se houver)\n"
            "- Retorne SOMENTE o texto, sem prefixos ou explicações\n\n"
            f"{base}"
        )
    # modo "completo" (padrão)
    return (
        "Você é um assistente especializado em redação profissional para orçamentos comerciais de iluminação.\n\n"
        "Melhore o texto abaixo seguindo estas regras:\n"
        "1. Corrija erros de digitação e gramática (português brasileiro)\n"
        "2. Organize em Markdown bem estruturado: use **negrito** para termos técnicos importantes, "
        "listas com hífen quando houver enumerações, parágrafos separados por linha em branco\n"
        "3. Mantenha tom profissional e formal, adequado para um documento comercial\n"
        "4. Preserve TODAS as informações técnicas originais — não invente nada que não esteja no texto\n"
        "5. Se o texto já estiver bem escrito, faça apenas ajustes mínimos necessários\n\n"
        "Retorne SOMENTE o texto melhorado em Markdown, sem explicações, sem prefixos como 'Aqui está:' ou similares.\n\n"
        f"Texto original:\n{texto}"
    )


@app.post("/api/ollama/melhorar-texto")
async def ollama_melhorar_texto(body: MelhorarTextoRequest, request: Request):
    session = _get_session(request)
    if not session:
        raise HTTPException(status_code=401, detail="Not authenticated")

    texto = body.texto.strip()
    modo  = body.modo if body.modo in ("minimo", "completo", "livre") else "completo"

    if modo != "livre" and not texto:
        raise HTTPException(status_code=400, detail="Texto vazio.")
    if modo == "livre" and not body.instrucao.strip() and not texto:
        raise HTTPException(status_code=400, detail="No modo livre informe uma instrução ou texto base.")

    prompt = _build_melhorar_prompt(texto, modo, body.instrucao.strip())

    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(
            OLLAMA_URL,
            json={"model": "llama3.1:8b", "prompt": prompt, "stream": False},
        )

    if not r.is_success:
        raise HTTPException(status_code=502, detail="Ollama não respondeu. Verifique se o serviço está ativo.")

    sugestao = r.json().get("response", "").strip()
    if not sugestao:
        raise HTTPException(status_code=502, detail="Ollama retornou resposta vazia.")

    return {"sugestao": sugestao}


class OrcamentoRequest(BaseModel):
    items: list[dict]
    total: float
    client_name: str
    client_cnpj: str = ""
    validade: str = ""
    descricao_projeto: str = ""
    observacoes: str = ""
    pagamento: list[dict] = []
    endereco: str = ""
    responsavel: str = ""
    telefone: str = ""
    email_contato: str = ""


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

    descricao_projeto_md = ""
    if body.descricao_projeto.strip():
        descricao_projeto_md = f"""\
## 📋 Descrição do Projeto

{body.descricao_projeto.strip()}

---

"""

    info_adicionais_md = ""
    if body.observacoes.strip():
        info_adicionais_md = f"""\
## 📝 Informações Adicionais

{body.observacoes.strip()}

---

"""

    # Linha 1 — data e validade, alinhadas à direita
    date_parts = [f"<strong>DATA:</strong> {today}"]
    if body.validade:
        date_parts.append(f"<strong>VALIDADE:</strong> {body.validade}")
    date_line_html = (
        '<div style="text-align:right; font-size:0.88em; margin-bottom:0.6em; color:#444;">'
        + " &nbsp;&nbsp;&nbsp; ".join(date_parts)
        + "</div>"
    )

    # Linhas restantes — à esquerda
    cnpj_line  = f"**CNPJ:** {body.client_cnpj}  \n" if body.client_cnpj  else ""
    end_line   = f"**ENDEREÇO:** {body.endereco}  \n" if body.endereco    else ""
    resp_line  = f"**RESPONSÁVEL:** {body.responsavel}  \n" if body.responsavel else ""

    # Telefone e e-mail na mesma linha
    contact_parts = []
    if body.telefone:    contact_parts.append(f"**Tel:** {body.telefone}")
    if body.email_contato: contact_parts.append(f"**E-mail:** {body.email_contato}")
    contact_line = "  &nbsp;&nbsp;|&nbsp;&nbsp;  ".join(contact_parts) + "  \n" if contact_parts else ""

    md_content = f"""# ORÇAMENTO DE ILUMINAÇÃO - {body.client_name.upper()}

{date_line_html}

**CLIENTE:** {body.client_name.upper()}
{cnpj_line}{end_line}{resp_line}{contact_line}
---

{descricao_projeto_md}## 📦 Itens do Orçamento

| Ambiente | Grupo | Qtd | Valor Unit. | Total |
| :--- | :--- | :---: | :--- | :--- |
{table_rows}
---

{pagamento_md}{info_adicionais_md}---

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
