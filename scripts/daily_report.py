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

def send_email(subject: str, body: str):
    import smtplib
    from email.mime.text import MIMEText
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"]    = EMAIL_USER
    msg["To"]      = EMAIL_USER

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
        send_email(f"[P.A. Construshop] Relatório {since} → {today}", report)


if __name__ == "__main__":
    main()
