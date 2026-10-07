# core/graph_auth.py — autenticação no Microsoft Graph (client credentials)
import os
import msal
from dotenv import load_dotenv

# Carrega as variáveis do arquivo .env
load_dotenv()

def get_graph_token():
    """
    Autentica no Microsoft Entra ID usando Client Credentials
    e retorna o Access Token para usar na Graph API.
    """
    tenant_id = os.getenv("AZURE_TENANT_ID")
    client_id = os.getenv("AZURE_CLIENT_ID")
    client_secret = os.getenv("AZURE_CLIENT_SECRET")

    if not all([tenant_id, client_id, client_secret]):
        raise ValueError("Erro: Credenciais do Azure não encontradas no .env")

    # A URL do diretório da sua empresa na Microsoft
    authority = f"https://login.microsoftonline.com/{tenant_id}"
    
    # O escopo padrão para aplicações em background na Graph API
    scopes = ["https://graph.microsoft.com/.default"]

    # Inicia o cliente MSAL
    app = msal.ConfidentialClientApplication(
        client_id,
        authority=authority,
        client_credential=client_secret
    )

    # Solicita o token
    result = app.acquire_token_for_client(scopes=scopes)

    if "access_token" in result:
        return result["access_token"]
    else:
        erro_desc = result.get("error_description", "Erro desconhecido")
        raise Exception(f"Falha na autenticação MSAL: {erro_desc}")