"""core/bloqueio_login.py — Fase 1: bloqueio do login de contas inativas via Microsoft Graph.

Permissões de aplicativo necessárias (com consentimento do admin):
  - User.ReadWrite.All  (accountEnabled + revokeSignInSessions)
  - LicenseAssignment.ReadWrite.All ou User.ReadWrite.All (assignLicense)
"""
import logging
from datetime import datetime

import pandas as pd
import requests

GRAPH = "https://graph.microsoft.com/v1.0"
log = logging.getLogger(__name__)

# Ajuste aos nomes reais das colunas do seu DataFrame
COL_EMAIL = "Email"
COL_TIPO = "Tipo_Caixa"
COL_CLASSIFICACAO = "Classificacao"
CLASSIFICACAO_ALVO = "CANDIDATA A BLOQUEIO"
COL_DIAS = "Dias_Inativa"
# Só estes tipos são bloqueados automaticamente ('Desconhecido' etc. exige revisão manual)
TIPOS_ELEGIVEIS = {"usermailbox", "sharedmailbox"}
DIAS_NUNCA_ACESSADA = 999  # valor que classificacao_contas usa quando não há atividade
MAX_ERROS_SEGUIDOS = 5  # interrompe o lote se houver muitos erros em sequência


def _headers(token):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _resultado(email, tipo, acao, status, detalhe=""):
    return {
        "Email": email,
        "Tipo_Caixa": tipo,
        "Acao": acao,
        "Resultado": status,  # BLOQUEADA | LICENCA_REMOVIDA | SIMULADO | PULADA | ERRO
        "Detalhe": detalhe,
        "Data_Acao": datetime.now().strftime("%d/%m/%Y %H:%M"),
    }


def bloquear_conta(token, email, tipo, dry_run=True, remover_licenca=False):
    """Bloqueia uma conta. Retorna sempre um dict (nunca levanta exceção)."""
    acao = "Bloquear login" + (" + remover licença" if remover_licenca else "")
    if dry_run:
        return _resultado(email, tipo, acao, "SIMULADO", "dry-run: nada foi alterado")

    h = _headers(token)
    passo = "consultar usuário"
    aviso = ""
    try:
        r = requests.get(
            f"{GRAPH}/users/{email}",
            headers=h,
            params={"$select": "id,accountEnabled,assignedLicenses,onPremisesSyncEnabled"},
            timeout=30,
        )
        r.raise_for_status()
        u = r.json()

        # Contas sincronizadas do AD local: o Graph não permite alterar accountEnabled
        if u.get("onPremisesSyncEnabled") and tipo != "SharedMailbox":
            return _resultado(email, tipo, acao, "PULADA",
                              "Conta sincronizada com AD local; bloquear no AD")

        skus = [l["skuId"] for l in u.get("assignedLicenses", [])]
        if not u.get("accountEnabled") and not (remover_licenca and skus):
            return _resultado(email, tipo, acao, "JA_BLOQUEADA", "Conta já estava bloqueada")

        status = "BLOQUEADA"
        if u.get("accountEnabled"):
            passo = "bloquear login (accountEnabled)"
            r = requests.patch(f"{GRAPH}/users/{email}", headers=h,
                               json={"accountEnabled": False}, timeout=30)
            r.raise_for_status()
            # Derruba sessões/tokens ativos. Se falhar, o login JÁ está bloqueado: não é erro fatal.
            passo = "derrubar sessões"
            rs = requests.post(f"{GRAPH}/users/{email}/revokeSignInSessions",
                               headers=h, timeout=30)
            if rs.status_code >= 400:
                aviso = f"Login bloqueado, mas não foi possível derrubar as sessões ({rs.status_code})"

        if remover_licenca:
            if skus:
                passo = "remover licença"
                r = requests.post(f"{GRAPH}/users/{email}/assignLicense", headers=h,
                                  json={"addLicenses": [], "removeLicenses": skus},
                                  timeout=30)
                r.raise_for_status()
                status = "LICENCA_REMOVIDA"

        return _resultado(email, tipo, acao, status, aviso)

    except requests.HTTPError as e:
        corpo = e.response.text[:300] if e.response is not None else str(e)
        log.error("Falha ao bloquear %s [%s]: %s", email, passo, corpo)
        return _resultado(email, tipo, acao, "ERRO", f"[{passo}] {corpo}")
    except Exception as e:  # noqa: BLE001
        log.exception("Erro inesperado em %s [%s]", email, passo)
        return _resultado(email, tipo, acao, "ERRO", f"[{passo}] {e}")


def processar_bloqueios(token, df, dry_run=True, limite_max=30, lote=None,
                        remover_licenca_usuarios=False, remover_licenca_shared=False,
                        permitir_nunca_acessadas=False):
    """Bloqueia todas as candidatas do DataFrame e devolve um DataFrame de resultados."""
    alvo = df[df[COL_CLASSIFICACAO] == CLASSIFICACAO_ALVO]
    alvo = alvo[alvo[COL_TIPO].astype(str).str.lower().isin(TIPOS_ELEGIVEIS)]
    if not permitir_nunca_acessadas:
        alvo = alvo[alvo[COL_DIAS] < DIAS_NUNCA_ACESSADA]

    if len(alvo) > limite_max:
        raise RuntimeError(
            f"{len(alvo)} candidatas excedem o limite de segurança ({limite_max}). "
            "Revise o relatório manualmente ou aumente o limite conscientemente."
        )

    # Mais antigas primeiro; 'lote' limita quantas contas são de fato alteradas por execução
    alvo = alvo.sort_values(COL_DIAS, ascending=False)

    resultados, alteradas, erros_seguidos = [], 0, 0
    for _, row in alvo.iterrows():
        if lote is not None and alteradas >= lote:
            break
        if erros_seguidos >= MAX_ERROS_SEGUIDOS:
            log.error("Interrompido: %d erros seguidos (possível problema sistêmico)", erros_seguidos)
            break
        remover = (remover_licenca_shared if row[COL_TIPO] == "SharedMailbox"
                   else remover_licenca_usuarios)
        res = bloquear_conta(token, row[COL_EMAIL], row[COL_TIPO],
                             dry_run=dry_run, remover_licenca=remover)
        resultados.append(res)
        # Só alterações efetivas (ou simuladas) gastam o lote; ERRO/PULADA/JA_BLOQUEADA não,
        # senão uma conta problemática travaria o lote em toda execução.
        if res["Resultado"] in ("BLOQUEADA", "LICENCA_REMOVIDA", "SIMULADO"):
            alteradas += 1
        erros_seguidos = erros_seguidos + 1 if res["Resultado"] == "ERRO" else 0
    return pd.DataFrame(resultados)
