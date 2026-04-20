import sys
import os
from pathlib import Path

# Adiciona o caminho do core para importar o wrapper
sys.path.append(str(Path(__file__).parent.parent))
from core.ollama_wrapper import OllamaWrapper

def analyze_protheus(repo_path):
    core = OllamaWrapper()
    repo_name = os.path.basename(repo_path)

    print(f"--- Iniciando Avaliação Local do Repositório: {repo_name} ---")

    # 1. Coleta informações da estrutura
    structure = []
    for root, dirs, files in os.walk(repo_path):
        if '.git' in dirs: dirs.remove('.git')
        level = root.replace(repo_path, '').count(os.sep)
        indent = ' ' * 4 * (level)
        structure.append(f"{indent}{os.path.basename(root)}/")
        for f in files[:3]: # Apenas 3 arquivos por pasta para não sobrecarregar
            structure.append(f"{indent}    - {f}")

    structure_text = "\n".join(structure[:50]) # Limita o tamanho da estrutura

    # 2. Lê alguns arquivos chave
    files_to_read = [
        "README.md",
        "app/Actions/UsersActions.tlpp",
        "app/CSV/CSVServices.tlpp"
    ]

    content_samples = ""
    for f_path in files_to_read:
        full_path = os.path.join(repo_path, f_path)
        if os.path.exists(full_path):
            with open(full_path, 'r', encoding='latin-1') as f: # Protheus costuma usar latin-1
                content_samples += f"\n--- ARQUIVO: {f_path} ---\n{f.read()[:1000]}\n"

    # 3. Prompt para o Llama
    prompt = f"""
Vocês é um Arquiteto de Software sênior especialista em Totvs Protheus e TLPP.
Analise as informações abaixo do repositório '{repo_name}' e traga os PRINCIPAIS ALTOS E BAIXOS do projeto.

ESTRUTURA DO PROJETO:
{structure_text}

AMOSTRAS DE CÓDIGO:
{content_samples}

Responda em Português, de forma direta e técnica, focando em:
- Arquitetura e OrganizaÃ§Ã£o.
- PadrÃµes de CÃ³digo (Clean Code).
- Possíveis gargalos ou pontos de melhoria.
"""

    analysis = core.ask(prompt)
    print("\n=== RESULTADO DA AVALIAÇÃO DA IA LOCAL ===")
    print(analysis)

if __name__ == "__main__":
    path = "/home/bruno/Documentos/Github/br.com.paconstrushop.protheus"
    analyze_protheus(path)
