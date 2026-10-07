"""core/conversao_e_licenca.py — Fase 2: bloquear login → converter em compartilhada → remover licença.

A ordem é garantida por conta:
  1. a conversão só roda se o bloqueio do login deu certo (ou já estava feito);
  2. a licença só é removida se a conversão foi CONFIRMADA e as verificações de segurança
     (tamanho, arquivo morto, holds) passaram.
Qualquer falha interrompe o pipeline daquela conta, sem afetar as demais.
"""
import json
import logging
import os
import subprocess
import tempfile

import pandas as pd
import requests

from core.bloqueio_login import (
    GRAPH, _headers, _resultado, bloquear_conta,
    COL_EMAIL, COL_TIPO, COL_CLASSIFICACAO, CLASSIFICACAO_ALVO, COL_DIAS,
    TIPOS_ELEGIVEIS, DIAS_NUNCA_ACESSADA, MAX_ERROS_SEGUIDOS,
)

log = logging.getLogger(__name__)

# <raiz do projeto>/scripts/converter_para_compartilhada.ps1
SCRIPT_PS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts", "converter_para_compartilhada.ps1",
)

# Resultados que representam alteração efetiva (ou simulada) e portanto gastam o lote
CONTAM_NO_LOTE = ("BLOQUEADA", "CONVERTIDA", "LICENCA_REMOVIDA", "SIMULADO")


def _acao_texto(tipo):
    if tipo == "SharedMailbox":
        return "Bloquear login → remover licença (já é compartilhada)"
    return "Bloquear login → converter em compartilhada → remover licença"


def _rodar_powershell(emails, somente_verificar):
    """Roda o script do Exchange UMA vez para todas as contas do lote. Retorna {email: resultado}."""
    app_id = os.getenv("EXO_APP_ID")
    thumbprint = os.getenv("EXO_THUMBPRINT")
    organizacao = os.getenv("EXO_ORGANIZATION")
    if not all([app_id, thumbprint, organizacao]):
        raise RuntimeError("Defina EXO_APP_ID, EXO_THUMBPRINT e EXO_ORGANIZATION no .env")
    if not os.path.exists(SCRIPT_PS):
        raise RuntimeError(f"Script não encontrado: {SCRIPT_PS}")

    ps = os.getenv("POWERSHELL_EXE", "powershell.exe")
    with tempfile.TemporaryDirectory() as tmp:
        entrada = os.path.join(tmp, "emails.json")
        saida = os.path.join(tmp, "saida.json")
        with open(entrada, "w", encoding="utf-8") as f:
            json.dump(emails, f)

        cmd = [ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", SCRIPT_PS,
               "-EmailsJson", entrada, "-SaidaJson", saida,
               "-AppId", app_id, "-Thumbprint", thumbprint, "-Organization", organizacao]
        if somente_verificar:
            cmd.append("-SomenteVerificar")

        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="oem" if os.name == "nt" else "utf-8", errors="replace",
                              timeout=120 * len(emails) + 300)
        if not os.path.exists(saida):
            raise RuntimeError(
                f"Script do Exchange falhou (código {proc.returncode}): "
                f"{(proc.stderr or proc.stdout)[:400]}"
            )
        with open(saida, encoding="utf-8-sig") as f:
            dados = json.load(f)

    if isinstance(dados, dict):
        dados = [dados]
    return {str(d["Email"]).lower(): d for d in dados}


def _remover_licencas(token, email):
    """Remove as licenças atribuídas DIRETAMENTE. Retorna (removidas, herdadas_de_grupo).
    Licenças herdadas de grupo não podem ser removidas por aqui (é preciso tirar a conta do grupo)."""
    h = _headers(token)
    r = requests.get(f"{GRAPH}/users/{email}", headers=h,
                     params={"$select": "licenseAssignmentStates"}, timeout=30)
    r.raise_for_status()
    estados = r.json().get("licenseAssignmentStates", [])
    diretas = sorted({e["skuId"] for e in estados if not e.get("assignedByGroup")})
    herdadas = sorted({e["skuId"] for e in estados if e.get("assignedByGroup")})
    if diretas:
        r = requests.post(f"{GRAPH}/users/{email}/assignLicense", headers=h,
                          json={"addLicenses": [], "removeLicenses": diretas}, timeout=30)
        r.raise_for_status()
    return len(diretas), len(herdadas)


def _status_parcial(convertida, bloqueou_agora, removidas):
    """Status quando o pipeline termina sem remover a licença."""
    if convertida:
        return "CONVERTIDA"
    if bloqueou_agora or removidas:
        return "BLOQUEADA"
    return "PULADA"  # nada mudou nesta execução


def _finalizar(token, email, tipo, bloqueou_agora, d, dry_run):
    acao = _acao_texto(tipo)
    if d is None:
        return _resultado(email, tipo, acao, "ERRO", "[exchange] sem retorno do script para esta conta")
    if d.get("Erro"):
        return _resultado(email, tipo, acao, "ERRO", f"[converter/verificar] {d['Erro']}")

    convertida = bool(d.get("Convertida"))
    pode = bool(d.get("PodeRemoverLicenca"))
    motivos = d.get("Motivos") or ""

    if dry_run:
        if pode:
            return _resultado(email, tipo, acao, "SIMULADO", "Passaria por todas as etapas e a licença seria removida")
        return _resultado(email, tipo, acao, "SIMULADO", f"Licença seria MANTIDA: {motivos}")

    if not pode:
        return _resultado(email, tipo, acao, _status_parcial(convertida, bloqueou_agora, 0),
                          f"Licença mantida: {motivos}")

    try:
        removidas, herdadas = _remover_licencas(token, email)
    except requests.HTTPError as e:
        corpo = e.response.text[:300] if e.response is not None else str(e)
        return _resultado(email, tipo, acao, "ERRO", f"[remover licença] {corpo}")
    except Exception as e:  # noqa: BLE001
        return _resultado(email, tipo, acao, "ERRO", f"[remover licença] {e}")

    if herdadas:
        return _resultado(email, tipo, acao, _status_parcial(convertida, bloqueou_agora, removidas),
                          "Licença herdada de grupo: remova a conta do grupo de licenciamento "
                          f"(licenças diretas removidas: {removidas})")
    if removidas == 0:
        return _resultado(email, tipo, acao, _status_parcial(convertida, bloqueou_agora, 0),
                          "Nenhuma licença direta encontrada para remover")
    return _resultado(email, tipo, acao, "LICENCA_REMOVIDA")


def _processar_chunk(token, chunk, dry_run):
    saida, seguem = [], {}

    # Passo 1: bloquear login (aqui NUNCA remove licença)
    for email, tipo in chunk:
        if dry_run:
            seguem[email] = (tipo, False)
            continue
        r1 = bloquear_conta(token, email, tipo, dry_run=False, remover_licenca=False)
        if r1["Resultado"] in ("BLOQUEADA", "JA_BLOQUEADA"):
            seguem[email] = (tipo, r1["Resultado"] == "BLOQUEADA")
        else:  # PULADA ou ERRO: não converte nem mexe em licença
            saida.append(_resultado(email, tipo, _acao_texto(tipo), r1["Resultado"],
                                    f"Passo 1 (bloqueio): {r1['Detalhe']} — pipeline interrompido para esta conta"))
    if not seguem:
        return saida, False

    # Passos 2 e 3: converter e verificar no Exchange (uma chamada para o lote inteiro)
    try:
        exo = _rodar_powershell(list(seguem), somente_verificar=dry_run)
    except Exception as e:  # noqa: BLE001
        log.error("Falha no script do Exchange: %s", e)
        sufixo = "" if dry_run else " (o login já foi bloqueado)"
        for email, (tipo, _) in seguem.items():
            saida.append(_resultado(email, tipo, _acao_texto(tipo), "ERRO", f"[exchange] {e}{sufixo}"))
        return saida, True  # falha sistêmica: quem chama deve parar

    # Passo 4: remover licença (só se a conversão foi confirmada e as verificações passaram)
    for email, (tipo, bloqueou_agora) in seguem.items():
        saida.append(_finalizar(token, email, tipo, bloqueou_agora, exo.get(email), dry_run))
    return saida, False


def processar_fase2(token, df, dry_run=True, limite_max=30, lote=None, permitir_nunca_acessadas=False):
    """Executa a fase 2 nas candidatas. Em dry_run só consulta (verificações no Exchange), sem alterar nada."""
    alvo = df[df[COL_CLASSIFICACAO] == CLASSIFICACAO_ALVO]
    alvo = alvo[alvo[COL_TIPO].astype(str).str.lower().isin(TIPOS_ELEGIVEIS)]
    if not permitir_nunca_acessadas:
        alvo = alvo[alvo[COL_DIAS] < DIAS_NUNCA_ACESSADA]

    if len(alvo) > limite_max:
        raise RuntimeError(
            f"{len(alvo)} candidatas excedem o limite de segurança ({limite_max}). "
            "Revise o relatório manualmente ou aumente o limite conscientemente."
        )

    alvo = alvo.sort_values(COL_DIAS, ascending=False)
    pendentes = [(r[COL_EMAIL], r[COL_TIPO]) for _, r in alvo.iterrows()]

    resultados, alteradas, erros_seguidos = [], 0, 0
    # Processa em blocos; contas que não alteram nada (PULADA/ERRO) não gastam o lote
    while pendentes and (lote is None or alteradas < lote):
        n = len(pendentes) if lote is None else lote - alteradas
        chunk, pendentes = pendentes[:n], pendentes[n:]
        res, falha_exchange = _processar_chunk(token, chunk, dry_run)
        resultados.extend(res)
        for r in res:
            if r["Resultado"] in CONTAM_NO_LOTE:
                alteradas += 1
            erros_seguidos = erros_seguidos + 1 if r["Resultado"] == "ERRO" else 0
        if falha_exchange:
            log.error("Interrompido: o script do Exchange falhou; as demais contas NÃO foram processadas")
            break
        if erros_seguidos >= MAX_ERROS_SEGUIDOS:
            log.error("Interrompido: %d erros seguidos (possível problema sistêmico)", erros_seguidos)
            break
    return pd.DataFrame(resultados)
