import requests
import sys
import os

def upload_to_redmine(issue_id, file_path, api_key, redmine_url, update_data=None):
    print(f"Subindo arquivo {file_path} para a tarefa {issue_id}...")
    
    # 1. Upload the file to get a token
    upload_url = f"{redmine_url}/uploads.json"
    headers = {
        "X-Redmine-API-Key": api_key,
        "Content-Type": "application/octet-stream"
    }
    
    with open(file_path, 'rb') as f:
        response = requests.post(upload_url, headers=headers, data=f)
    
    if response.status_code != 201:
        print(f"Erro no upload: {response.text}")
        return
    
    token = response.json()['upload']['token']
    print(f"Upload concluído. Token: {token}")
    
    # 2. Attach the token to the issue
    issue_url = f"{redmine_url}/issues/{issue_id}.json"
    
    if update_data:
        # Update the token in the provided data
        update_data["issue"]["uploads"][0]["token"] = token
    else:
        update_data = {
            "issue": {
                "notes": "Upload de arquivos.",
                "uploads": [
                    {
                        "token": token,
                        "filename": os.path.basename(file_path),
                        "description": "Arquivo anexado",
                        "content_type": "application/octet-stream"
                    }
                ]
            }
        }
    
    headers = {
        "X-Redmine-API-Key": api_key,
        "Content-Type": "application/json"
    }
    
    response = requests.put(issue_url, headers=headers, json=update_data)
    
    if response.status_code == 200 or response.status_code == 204:
        print("Tarefa atualizada com sucesso!")
    else:
        print(f"Erro ao atualizar tarefa: {response.text}")

if __name__ == "__main__":
    API_KEY = "e177dad8a34d1613f7b57358edca34b5711897b9"
    URL = "https://tasks.jarvis.paconstrushop.com.br"
    
    # DADOS DO JB
    ISSUE_ID = 8871
    FILE = "/home/bruno/Documentos/Github/protocolo_contestacao_JB_v2.zip"
    
    update_info = {
        "issue": {
            "notes": "CONTESTAÇÃO TÉCNICA DE VISTORIA - JB (CORPORATIVA TELECOM)\n\n**Argumentação:**\n1. A vistoria realizada em 17/04 alegou incorretamente um disjuntor de 3x63A.\n2. Conforme o formulário de projeto aprovado e o 'Termo de Opção de Atendimento em BT' (anexos), a solicitação correta é de **3x800A**.\n3. Base Legal: **Art. 70 da REN 1.000/2021** (Dever de fundamentação técnica) e **ND 5.1**.\n\nSolicitamos a reanálise imediata com base no projeto original aprovado.",
            "uploads": [
                {
                    "token": "",
                    "filename": os.path.basename(FILE),
                    "description": "Dossiê completo para contestação de vistoria JB v2",
                    "content_type": "application/zip"
                }
            ]
        }
    }
    
    upload_to_redmine(ISSUE_ID, FILE, API_KEY, URL, update_info)
