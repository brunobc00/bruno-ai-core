from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
import subprocess
import httpx
import os

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
        # Atualiza refs remotas silenciosamente
        subprocess.run(["git", "-C", str(d), "fetch", "--quiet"],
                       capture_output=True)
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


# ── Static (must be last) ─────────────────────────────────────────────────────
app.mount("/", StaticFiles(directory=DOCS_DIR, html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
