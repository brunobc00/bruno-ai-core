import os
from pathlib import Path
import markdown

def update_hub():
    # Caminhos
    base_dir = Path('/home/bruno/Documentos/Github')
    hub_path = base_dir / 'bruno-ai-core/docs/hub/index.html'
    rules_path = base_dir / 'WORKSPACE_RULES.md'
    norms_index_path = base_dir / 'cemig/normas/INDEX.md'

    print("--- Sincronizando Antigravity Docs Hub ---")

    # 1. Lê o HTML original
    with open(hub_path, 'r', encoding='utf-8') as f:
        html_content = f.read()

    # 2. Processa WORKSPACE_RULES.md
    if rules_path.exists():
        with open(rules_path, 'r', encoding='utf-8') as f:
            rules_md = f.read()
        rules_html = markdown.markdown(rules_md, extensions=['tables', 'fenced_code'])
        
        start_marker = "<!-- RULES_START -->"
        end_marker = "<!-- RULES_END -->"
        
        pre = html_content.split(start_marker)[0]
        post = html_content.split(end_marker)[1]
        html_content = f"{pre}{start_marker}\n{rules_html}\n{end_marker}{post}"
        print(f"✅ Regras sincronizadas ({rules_path.name})")

    # 3. Processa Normas INDEX.md
    if norms_index_path.exists():
        with open(norms_index_path, 'r', encoding='utf-8') as f:
            norms_md = f.read()
        norms_html = markdown.markdown(norms_md, extensions=['tables', 'fenced_code'])
        
        start_marker = "<!-- NORMS_START -->"
        end_marker = "<!-- NORMS_END -->"
        
        pre = html_content.split(start_marker)[0]
        post = html_content.split(end_marker)[1]
        html_content = f"{pre}{start_marker}\n{norms_html}\n{end_marker}{post}"
        print(f"✅ Índice de Normas sincronizado ({norms_index_path.name})")

    # 4. Salva o HTML atualizado
    with open(hub_path, 'w', encoding='utf-8') as f:
        f.write(html_content)
    
    print(f"\n🚀 Hub atualizado com sucesso! Abra: file://{hub_path}")

if __name__ == "__main__":
    update_hub()
