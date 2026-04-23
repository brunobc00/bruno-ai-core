#!/usr/bin/env python3
"""
daily_report.py — Relatório diário de atividades

Analisa commits Git e tarefas Redmine desde uma data base
e gera um resumo executivo por email ou terminal.

Uso:
    python3 scripts/daily_report.py              # desde ontem
    python3 scripts/daily_report.py --since 2026-04-18   # desde data específica
    python3 scripts/daily_report.py --send-email
"""

import argparse
import os
import subprocess
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv("/home/bruno/Documentos/Github/.env")

WORKSPACE   = Path("/home/bruno/Documentos/Github")
REDMINE_URL = os.getenv("REDMINE_URL")
REDMINE_KEY = os.getenv("REDMINE_API_KEY")
EMAIL_USER  = os.getenv("EMAIL_USER")
EMAIL_PASS  = os.getenv("EMAIL_PASS")


# ── Git ───────────────────────────────────────────────────────────────────────

def git_report(since: str) -> dict:
    repos = {}
    for d in sorted(WORKSPACE.iterdir()):
        if not (d / ".git").exists():
            continue
        result = subprocess.run(
            ["git", "-C", str(d), "log", "--oneline",
             f"--after={since} 00:00",
             "--format=%h|%ad|%an|%s", "--date=short"],
            capture_output=True, text=True
        )
        lines = [l for l in result.stdout.strip().splitlines() if l]
        if not lines:
            continue

        commits = []
        for line in lines:
            parts = line.split("|", 3)
            if len(parts) == 4:
                commits.append({
                    "hash": parts[0], "date": parts[1],
                    "author": parts[2], "msg": parts[3]
                })
        if commits:
            repos[d.name] = commits

    return repos


def format_git_section(repos: dict, since: str) -> str:
    if not repos:
        return "Nenhuma alteração nos repositórios.\n"

    total = sum(len(c) for c in repos.values())
    lines = [f"**{len(repos)} repositórios** com atividade — {total} commit(s) desde {since}\n"]

    for repo, commits in repos.items():
        lines.append(f"\n📁 {repo} ({len(commits)} commit(s))")
        for c in commits:
            lines.append(f"   {c['date']} [{c['author']}] {c['msg']}")

    return "\n".join(lines)


# ── Redmine ───────────────────────────────────────────────────────────────────

def redmine_report(since: str) -> dict:
    until = (date.today() + timedelta(days=1)).isoformat()
    url = (f"{REDMINE_URL}/issues.json"
           f"?updated_on=><{since}|{until}&limit=100&status_id=*")

    resp = httpx.get(url, headers={"X-Redmine-API-Key": REDMINE_KEY}, timeout=15)
    resp.raise_for_status()
    issues = resp.json().get("issues", [])

    by_person   = defaultdict(list)
    by_status   = defaultdict(int)
    by_project  = defaultdict(int)
    new_today   = []
    resolved    = []

    today = date.today().isoformat()

    for i in issues:
        person  = i.get("assigned_to", {}).get("name", "Sem responsável")
        status  = i["status"]["name"]
        project = i.get("project", {}).get("name", "?")
        created = i.get("created_on", "")[:10]

        by_person[person].append(i)
        by_status[status] += 1
        by_project[project] += 1

        if created == today:
            new_today.append(i)
        if status in ("Resolved", "Closed"):
            resolved.append(i)

    return {
        "issues":     issues,
        "by_person":  dict(by_person),
        "by_status":  dict(by_status),
        "by_project": dict(by_project),
        "new_today":  new_today,
        "resolved":   resolved,
    }


def format_redmine_section(data: dict, since: str) -> str:
    issues    = data["issues"]
    by_person = data["by_person"]
    by_status = data["by_status"]
    new_today = data["new_today"]
    resolved  = data["resolved"]

    if not issues:
        return "Nenhuma atividade no Redmine.\n"

    lines = [f"**{len(issues)} tarefas** movimentadas desde {since}\n"]

    # Resumo por status
    lines.append("Status:")
    for s, n in sorted(by_status.items(), key=lambda x: -x[1]):
        lines.append(f"   {s}: {n}")

    # Por pessoa
    lines.append("\nPor responsável:")
    for person, tasks in sorted(by_person.items(), key=lambda x: -len(x[1])):
        statuses = defaultdict(int)
        for t in tasks:
            statuses[t["status"]["name"]] += 1
        status_str = "  ".join(f"{s}:{n}" for s, n in statuses.items())
        lines.append(f"   {person}: {len(tasks)} tarefas  ({status_str})")

    # Novas hoje
    if new_today:
        lines.append(f"\nAbertas hoje ({len(new_today)}):")
        for t in new_today:
            person = t.get("assigned_to", {}).get("name", "—")
            lines.append(f"   #{t['id']} [{t.get('project',{}).get('name','?')}] {t['subject'][:65]}  → {person}")

    # Resolvidas/fechadas
    if resolved:
        lines.append(f"\nConcluídas ({len(resolved)}):")
        for t in resolved:
            person = t.get("assigned_to", {}).get("name", "—")
            lines.append(f"   #{t['id']} [{t['status']['name']}] {t['subject'][:65]}  → {person}")

    # Tarefas sem responsável
    sem = data["by_person"].get("Sem responsável", [])
    if sem:
        new_sem = [t for t in sem if t["status"]["name"] == "New"]
        if new_sem:
            lines.append(f"\n⚠ {len(new_sem)} tarefas novas sem responsável:")
            for t in new_sem[:5]:
                lines.append(f"   #{t['id']} [{t.get('project',{}).get('name','?')}] {t['subject'][:65]}")
            if len(new_sem) > 5:
                lines.append(f"   ... e mais {len(new_sem)-5}")

    return "\n".join(lines)


# ── Email ─────────────────────────────────────────────────────────────────────

def build_html(since: str, today: str, git_data: dict, redmine_data: dict) -> str:
    """Gera o HTML do email com visão por pessoa."""

    # Mapeia autor git → commits por repo
    git_by_author = defaultdict(list)
    for repo, commits in git_data.items():
        for c in commits:
            git_by_author[c["author"]].append({**c, "repo": repo})

    # Pessoas únicas (union de git + redmine, exceto 'Sem responsável')
    redmine_people = {p for p in redmine_data["by_person"] if p != "Sem responsável"}
    git_people     = set(git_by_author.keys())
    all_people     = sorted(redmine_people | git_people)

    status_colors = {
        "New":         ("#e3f0ff", "#1a6fc4"),
        "In Progress": ("#fff4e0", "#c47a00"),
        "Resolved":    ("#e3f9e5", "#1a8c2a"),
        "Closed":      ("#f0f0f0", "#666"),
    }

    def badge(status):
        bg, fg = status_colors.get(status, ("#eee", "#333"))
        return (f'<span style="background:{bg};color:{fg};padding:2px 8px;'
                f'border-radius:10px;font-size:11px;font-weight:600">{status}</span>')

    total_commits = sum(len(c) for c in git_data.values())
    total_issues  = len(redmine_data["issues"])
    total_done    = len(redmine_data["resolved"])
    sem_resp      = len([t for t in redmine_data["by_person"].get("Sem responsável", [])
                         if t["status"]["name"] == "New"])

    people_html = ""
    for person in all_people:
        redmine_tasks = redmine_data["by_person"].get(person, [])
        git_commits   = git_by_author.get(person, [])
        if not redmine_tasks and not git_commits:
            continue

        # Redmine rows
        redmine_rows = ""
        for t in redmine_tasks:
            status = t["status"]["name"]
            proj   = t.get("project", {}).get("name", "?")
            redmine_rows += (
                f'<tr><td style="color:#aaa;width:50px">#{t["id"]}</td>'
                f'<td style="color:#888;width:140px">{proj}</td>'
                f'<td>{t["subject"]}</td>'
                f'<td style="white-space:nowrap">{badge(status)}</td></tr>'
            )

        # Git rows (agrupa por repo)
        git_by_repo = defaultdict(list)
        for c in git_commits:
            git_by_repo[c["repo"]].append(c)

        git_rows = ""
        for repo, commits in sorted(git_by_repo.items()):
            for c in commits:
                git_rows += (
                    f'<tr><td style="color:#aaa;font-family:monospace;width:60px">{c["hash"]}</td>'
                    f'<td style="color:#CC1417;width:200px">📁 {repo}</td>'
                    f'<td>{c["msg"]}</td>'
                    f'<td style="color:#aaa;white-space:nowrap;width:80px">{c["date"]}</td></tr>'
                )

        people_html += f"""
        <div style="margin-bottom:24px;border:1px solid #eee;border-radius:8px;overflow:hidden">
          <div style="background:#f7f7f7;padding:12px 18px;border-bottom:1px solid #eee;display:flex;align-items:center;gap:12px">
            <div style="width:36px;height:36px;background:#CC1417;color:white;border-radius:50%;
                        display:flex;align-items:center;justify-content:center;font-weight:700;font-size:15px">
              {person[0].upper()}
            </div>
            <div>
              <div style="font-weight:700;font-size:15px">{person}</div>
              <div style="font-size:12px;color:#888">
                {len(redmine_tasks)} tarefa(s) no Redmine &nbsp;·&nbsp; {len(git_commits)} commit(s) no Git
              </div>
            </div>
          </div>
          {"" if not redmine_tasks else f'''
          <div style="padding:12px 18px 4px">
            <div style="font-size:11px;font-weight:700;color:#888;text-transform:uppercase;
                        letter-spacing:0.5px;margin-bottom:8px">Redmine</div>
            <table style="width:100%;border-collapse:collapse;font-size:13px">
              {redmine_rows}
            </table>
          </div>'''}
          {"" if not git_commits else f'''
          <div style="padding:12px 18px 4px">
            <div style="font-size:11px;font-weight:700;color:#888;text-transform:uppercase;
                        letter-spacing:0.5px;margin-bottom:8px">Git</div>
            <table style="width:100%;border-collapse:collapse;font-size:12px;font-family:monospace">
              {git_rows}
            </table>
          </div>'''}
          <div style="height:12px"></div>
        </div>"""

    sem_html = ""
    sem_tasks = [t for t in redmine_data["by_person"].get("Sem responsável", [])
                 if t["status"]["name"] == "New"]
    if sem_tasks:
        rows = "".join(
            f'<tr><td style="color:#aaa;width:50px">#{t["id"]}</td>'
            f'<td style="color:#888;width:140px">{t.get("project",{}).get("name","?")}</td>'
            f'<td>{t["subject"]}</td></tr>'
            for t in sem_tasks
        )
        sem_html = f"""
        <div style="background:#fff8e1;border:1px solid #ffd54f;border-radius:8px;padding:14px 18px;margin-bottom:24px">
          <div style="font-weight:700;color:#7a5c00;margin-bottom:10px">
            ⚠ {len(sem_tasks)} tarefas sem responsável
          </div>
          <table style="width:100%;border-collapse:collapse;font-size:13px">{rows}</table>
        </div>"""

    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:20px;background:#f4f4f4;font-family:Arial,sans-serif;color:#333">
<div style="max-width:720px;margin:0 auto">

  <!-- Header -->
  <div style="background:#CC1417;color:white;padding:24px 28px;border-radius:8px 8px 0 0">
    <div style="font-size:20px;font-weight:700">Relatório de Atividades</div>
    <div style="font-size:13px;opacity:.85;margin-top:4px">{since} → {today} &nbsp;·&nbsp; P.A. Construshop</div>
  </div>

  <!-- KPIs -->
  <div style="background:white;padding:20px 28px;display:flex;gap:12px;border-bottom:1px solid #eee">
    {"".join(f'<div style="flex:1;background:#f9f9f9;border:1px solid #eee;border-radius:6px;padding:12px;text-align:center"><div style="font-size:26px;font-weight:700;color:#CC1417">{n}</div><div style="font-size:11px;color:#888;margin-top:2px">{l}</div></div>'
             for n, l in [(total_commits,"Commits"), (len(git_data),"Repos ativos"),
                          (total_issues,"Tarefas Redmine"), (total_done,"Concluídas")])}
  </div>

  <!-- Pessoas -->
  <div style="background:white;padding:20px 28px;border-radius:0 0 8px 8px">
    <div style="font-size:13px;font-weight:700;color:#CC1417;text-transform:uppercase;
                letter-spacing:.5px;margin-bottom:16px">Por Responsável</div>
    {people_html}
    {sem_html}
  </div>

  <div style="text-align:center;font-size:11px;color:#bbb;margin-top:12px">
    Gerado por bruno-ai-core/scripts/daily_report.py
  </div>
</div>
</body></html>"""


def send_email(subject: str, html: str):
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = EMAIL_USER
    msg["To"]      = EMAIL_USER
    msg.attach(MIMEText(html, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(EMAIL_USER, EMAIL_PASS)
        smtp.send_message(msg)
    print(f"Email enviado para {EMAIL_USER}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", default=(date.today() - timedelta(days=1)).isoformat(),
                        help="Data de início YYYY-MM-DD (padrão: ontem)")
    parser.add_argument("--send-email", action="store_true")
    args = parser.parse_args()

    since = args.since
    today = date.today().isoformat()

    print(f"Gerando relatório desde {since}...\n")

    git_data     = git_report(since)
    redmine_data = redmine_report(since)

    git_txt     = format_git_section(git_data, since)
    redmine_txt = format_redmine_section(redmine_data, since)

    report = f"""╔══════════════════════════════════════════════════════╗
║       RELATÓRIO DE ATIVIDADES — {today}        ║
╚══════════════════════════════════════════════════════╝

━━━ GIT ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{git_txt}

━━━ REDMINE ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{redmine_txt}
"""

    print(report)

    if args.send_email:
        html = build_html(since, today, git_data, redmine_data)
        send_email(f"[P.A. Construshop] Relatório {since} → {today}", html)


if __name__ == "__main__":
    main()
