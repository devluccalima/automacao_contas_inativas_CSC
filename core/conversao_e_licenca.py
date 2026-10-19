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
import threading
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

from core.bloqueio_login import (
    GRAPH, _headers, _resultado, bloquear_conta,
    COL_EMAIL, COL_TIPO, COL_CLASSIFICACAO, CLASSIFICACAO_ALVO, COL_DIAS,
    TIPOS_ELEGIVEIS, MAX_ERROS_SEGUIDOS,
    COL_ESTAGIO, ESTAGIO_BLOQUEAR, ESTAGIO_COMPLETO,
)

log = logging.getLogger(__name__)

# <raiz do projeto>/scripts/converter_para_compartilhada.ps1
SCRIPT_PS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts", "converter_para_compartilhada.ps1",
)

# Trava de segurança: atividade registrada no Exchange (logon ou interação do usuário) nos últimos N dias
# impede QUALQUER ação sobre a conta. O relatório de uso do Graph chega com ~2 dias de atraso; esta leitura é atual.
DIAS_ATIVIDADE_RECENTE = 14

# Campos do Exchange que contam como atividade (nome no resultado do script, descrição). O LastUserActionTime fica de fora
# porque atrasa. Se o logon gerar muitas contas puladas (processos em segundo plano também o atualizam), remova a linha
# do UltimoLogon e fique só com a interação do usuário.
CAMPOS_ATIVIDADE_EXCHANGE = (
    ("UltimaInteracao", "última interação"),
    ("UltimoLogon", "último logon"),
)

# Resultados que representam alteração efetiva (ou simulada) e portanto gastam o lote
CONTAM_NO_LOTE = ("BLOQUEADA", "CONVERTIDA", "LICENCA_REMOVIDA", "SIMULADO")


def _converter_data_utc(texto):
    try:
        return datetime.strptime(texto, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def atividade_recente(dados_exchange, agora=None, dias=None):
    """Retorna (True, descrição) se o Exchange registrou logon ou interação do usuário nos últimos
    DIAS_ATIVIDADE_RECENTE dias. dados_exchange é o resultado do script converter_para_compartilhada.ps1.
    A descrição lista TODOS os sinais recentes e avisa quando só houve logon (sem interação do usuário)."""
    agora = agora or datetime.now(timezone.utc)
    limite = timedelta(days=dias or DIAS_ATIVIDADE_RECENTE)
    recentes = []
    for campo, rotulo in CAMPOS_ATIVIDADE_EXCHANGE:
        momento = _converter_data_utc((dados_exchange or {}).get(campo))
        # O ano 1601 é o valor do Exchange para "nunca": não conta
        if momento and momento.year > 1900 and agora - momento <= limite:
            recentes.append((campo, rotulo, momento))
    if not recentes:
        return False, ""
    partes = [f"{rotulo} em {momento.astimezone().strftime('%d/%m/%Y %H:%M')}" for _, rotulo, momento in recentes]
    if all(campo != "UltimaInteracao" for campo, _, _ in recentes):
        partes.append("somente logon, sem interação do usuário")
    return True, "; ".join(partes) + " (Exchange)"


def _acao_texto(tipo):
    if tipo == "SharedMailbox":
        return "Bloquear login → remover licença (já é compartilhada)"
    return "Bloquear login → converter em compartilhada → remover licença"


def _executar_com_progresso(cmd, timeout):
    """Roda o comando mostrando a saída em tempo real (o Exchange leva vários minutos para muitas contas).
    Devolve (código de saída, texto). Mata o processo se estourar o tempo ou se você apertar Ctrl+C."""
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
        encoding="oem" if os.name == "nt" else "utf-8", errors="replace",
    )
    linhas = []

    def ler():
        for linha in proc.stdout:
            linhas.append(linha)
            print("   [exchange]", linha.rstrip(), flush=True)

    leitor = threading.Thread(target=ler, daemon=True)
    leitor.start()
    try:
        proc.wait(timeout=timeout)
    except BaseException:  # tempo esgotado, Ctrl+C...
        proc.kill()
        raise
    leitor.join(timeout=10)
    return proc.returncode, "".join(linhas)


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

        codigo, texto = _executar_com_progresso(cmd, timeout=120 * len(emails) + 300)
        if not os.path.exists(saida):
            raise RuntimeError(f"Script do Exchange falhou (código {codigo}): {texto[-400:]}")
        with open(saida, encoding="utf-8-sig") as f:
            dados = json.load(f)

    if isinstance(dados, dict):
        dados = [dados]
    return {str(d["Email"]).lower(): d for d in dados}


def _licencas_do_usuario(token, email):
    """Lê do Entra ID as licenças do usuário. Retorna (diretas, herdadas_de_grupo) como listas de skuId."""
    r = requests.get(f"{GRAPH}/users/{email}", headers=_headers(token),
                     params={"$select": "licenseAssignmentStates"}, timeout=30)
    r.raise_for_status()
    estados = r.json().get("licenseAssignmentStates", [])
    diretas = sorted({e["skuId"] for e in estados if not e.get("assignedByGroup")})
    herdadas = sorted({e["skuId"] for e in estados if e.get("assignedByGroup")})
    return diretas, herdadas


def _remover_licencas(token, email, tentativas=6, espera=5):
    """Remove as licenças atribuídas DIRETAMENTE e CONFIRMA a remoção relendo o usuário
    (o Entra ID pode levar alguns segundos para refletir a mudança).
    Retorna (removidas, herdadas_de_grupo, confirmada).
    Licenças herdadas de grupo não podem ser removidas por aqui (é preciso tirar a conta do grupo)."""
    diretas, herdadas = _licencas_do_usuario(token, email)
    if not diretas:
        return 0, len(herdadas), True

    r = requests.post(f"{GRAPH}/users/{email}/assignLicense", headers=_headers(token),
                      json={"addLicenses": [], "removeLicenses": diretas}, timeout=30)
    r.raise_for_status()

    confirmada = False
    for _ in range(tentativas):
        restantes, herdadas = _licencas_do_usuario(token, email)
        if not restantes:
            confirmada = True
            break
        time.sleep(espera)
    return len(diretas), len(herdadas), confirmada


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

    # Trava final: a licença só pode sair se a caixa estiver CONFIRMADAMENTE compartilhada
    if d.get("TipoFinal") != "SharedMailbox":
        return _resultado(email, tipo, acao, "ERRO",
                          f"[conversão] tipo atual da caixa: {d.get('TipoFinal')}; licença NÃO removida")

    if not pode:
        return _resultado(email, tipo, acao, _status_parcial(convertida, bloqueou_agora, 0),
                          f"Licença mantida: {motivos}")

    try:
        removidas, herdadas, confirmada = _remover_licencas(token, email)
    except requests.HTTPError as e:
        corpo = e.response.text[:300] if e.response is not None else str(e)
        return _resultado(email, tipo, acao, "ERRO", f"[remover licença] {corpo}")
    except Exception as e:  # noqa: BLE001
        return _resultado(email, tipo, acao, "ERRO", f"[remover licença] {e}")

    if removidas and not confirmada:
        return _resultado(email, tipo, acao, _status_parcial(convertida, bloqueou_agora, removidas),
                          "Remoção da licença solicitada, mas ainda não confirmada no Entra ID: confira em Licenças")
    if herdadas:
        return _resultado(email, tipo, acao, _status_parcial(convertida, bloqueou_agora, removidas),
                          "Licença herdada de grupo: remova a conta do grupo de licenciamento "
                          f"(licenças diretas removidas: {removidas})")
    if removidas == 0:
        return _resultado(email, tipo, acao, _status_parcial(convertida, bloqueou_agora, 0),
                          "Nenhuma licença direta encontrada para remover")
    return _resultado(email, tipo, acao, "LICENCA_REMOVIDA")


def _login_ativo(token, email):
    """True/False conforme o accountEnabled atual no Entra ID. None se não foi possível ler (a trava continua valendo)."""
    try:
        r = requests.get(f"{GRAPH}/users/{email}", headers=_headers(token),
                         params={"$select": "accountEnabled"}, timeout=30)
        r.raise_for_status()
        return r.json().get("accountEnabled")
    except Exception:  # noqa: BLE001
        return None


def _checar_atividade(token, itens, acao=None):
    """Passo 0 (trava): leitura SOMENTE LEITURA da atividade atual no Exchange para uma lista de (email, tipo, estagio).
    Conta com atividade recente não é tocada, mesmo que o relatório de uso (atrasado) a mostre inativa ou nunca acessada.
    Devolve (liberadas, resultados, notas, pre, falha): as contas que seguem, os resultados das que ficaram de fora
    (PULADA/ERRO), observações para o Detalhe, a leitura bruta do Exchange e se o script do Exchange falhou."""
    resultados, notas, liberadas = [], {}, []
    try:
        pre = _rodar_powershell([e for e, _, _ in itens], somente_verificar=True)
    except Exception as e:  # noqa: BLE001
        log.error("Falha no script do Exchange (verificação de atividade): %s", e)
        for email, tipo, _ in itens:
            resultados.append(_resultado(email, tipo, acao or _acao_texto(tipo), "ERRO",
                                         f"[exchange] verificação de atividade falhou: {e} (a conta não foi alterada)"))
        return [], resultados, notas, {}, True
    for email, tipo, estagio in itens:
        d = pre.get(email.lower())
        if d is None or d.get("Erro"):
            motivo = "sem retorno do Exchange" if d is None else d["Erro"]
            resultados.append(_resultado(email, tipo, acao or _acao_texto(tipo), "ERRO",
                                         f"[verificação] {motivo} (a conta não foi alterada)"))
            continue
        recente, descricao = atividade_recente(d)
        if recente:
            # A trava protege quem ainda PODE usar a conta. Com o login já bloqueado o dono não entra: a atividade
            # vem de delegados ou processos e não impede converter em compartilhada nem tirar a licença (os
            # delegados continuam acessando; tamanho, arquivo morto e hold são verificados adiante). Se o login
            # não puder ser lido, a trava vale.
            if _login_ativo(token, email) is not False:
                resultados.append(_resultado(email, tipo, acao or _acao_texto(tipo), "PULADA",
                                             f"Atividade recente: {descricao}. Conta NÃO alterada"))
                continue
            notas[email] = f"Atividade de delegados ou processos ignorada (login já bloqueado): {descricao}"
        liberadas.append((email, tipo, estagio))
    return liberadas, resultados, notas, pre, False


def aplicar_trava_fase1(token, df, limite_max=30, bloco=25):
    """Trava de atividade recente para a FASE 1 (só bloqueia o login). Lê o Exchange (somente leitura) das candidatas e
    separa as protegidas. Devolve (df_para_bloquear, df_resultados_da_trava): o primeiro segue para
    processar_bloqueios; o segundo traz as contas PULADA/ERRO, que NÃO devem ser alteradas.
    Se o script do Exchange falhar, nenhuma conta segue (falha segura)."""
    alvo = df[df[COL_CLASSIFICACAO] == CLASSIFICACAO_ALVO]
    alvo = alvo[alvo[COL_TIPO].astype(str).str.lower().isin(TIPOS_ELEGIVEIS)]
    if len(alvo) > limite_max:  # mesma regra de processar_bloqueios, aplicada antes de ler o Exchange
        raise RuntimeError(
            f"{len(alvo)} candidatas excedem o limite de segurança ({limite_max}). "
            "Revise o relatório manualmente ou aumente o limite conscientemente."
        )
    itens = [(r[COL_EMAIL], r[COL_TIPO], ESTAGIO_BLOQUEAR) for _, r in alvo.iterrows()]
    resultados, excluir = [], set()
    for k in range(0, len(itens), bloco):
        parte = itens[k:k + bloco]
        print(f"   [trava] lendo a atividade no Exchange: contas {k + 1} a {k + len(parte)} de {len(itens)}", flush=True)
        _, res, _, _, falha = _checar_atividade(token, parte, acao="Bloquear login")
        resultados.extend(res)
        excluir.update(r["Email"] for r in res)
        if falha:
            restantes = itens[k + len(parte):]
            for email, tipo, _ in restantes:
                resultados.append(_resultado(email, tipo, "Bloquear login", "ERRO",
                                             "[exchange] verificação de atividade não executada (falha anterior); conta não alterada"))
                excluir.add(email)
            break
    return df[~df[COL_EMAIL].isin(excluir)].copy(), pd.DataFrame(resultados)


def _processar_chunk(token, chunk, dry_run, verificar_atividade=True):
    saida, seguem = [], {}
    notas = {}  # e-mail -> observação que será anexada ao Detalhe do resultado final

    def _anexar_notas(itens):
        for r in itens:
            nota = notas.get(r["Email"])
            if nota:
                r["Detalhe"] = f"{r['Detalhe']} | {nota}" if r["Detalhe"] else nota
        return itens

    # Passo 0 (trava): leitura somente leitura da atividade atual no Exchange (ver _checar_atividade)
    pre = {}
    if verificar_atividade:
        chunk, resultados_trava, notas_trava, pre, falha = _checar_atividade(token, chunk)
        saida.extend(resultados_trava)
        notas.update(notas_trava)
        if falha:
            return saida, True

    # Passo 1: bloquear login (aqui NUNCA remove licença)
    for email, tipo, estagio in chunk:
        # Estágio BLOQUEAR: abaixo do limite de conversão, só bloqueia o login (não converte, não mexe na licença)
        if estagio != ESTAGIO_COMPLETO:
            if dry_run:
                saida.append(_resultado(email, tipo, "Bloquear login", "SIMULADO",
                                        "Só bloquear o login (abaixo do limite de conversão)"))
            else:
                saida.append(bloquear_conta(token, email, tipo, dry_run=False, remover_licenca=False))
            continue
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
        return _anexar_notas(saida), False

    # Passos 2 e 3: converter e verificar no Exchange (uma chamada para o lote inteiro).
    # Em simulação, a leitura do passo 0 já traz tudo o que é preciso: não repete a conexão.
    try:
        if dry_run and pre:
            exo = {e: pre[e.lower()] for e in seguem if e.lower() in pre}
        else:
            exo = _rodar_powershell(list(seguem), somente_verificar=dry_run)
    except Exception as e:  # noqa: BLE001
        log.error("Falha no script do Exchange: %s", e)
        sufixo = "" if dry_run else " (o login já foi bloqueado)"
        for email, (tipo, _) in seguem.items():
            saida.append(_resultado(email, tipo, _acao_texto(tipo), "ERRO", f"[exchange] {e}{sufixo}"))
        return _anexar_notas(saida), True  # falha sistêmica: quem chama deve parar

    # Passo 4: remover licença (só se a conversão foi confirmada e as verificações passaram)
    for email, (tipo, bloqueou_agora) in seguem.items():
        saida.append(_finalizar(token, email, tipo, bloqueou_agora, exo.get(email), dry_run))
    return _anexar_notas(saida), False


def processar_fase2(token, df, dry_run=True, limite_max=30, lote=None, verificar_atividade=True):
    """Executa a fase 2 nas candidatas. Em dry_run só consulta (verificações no Exchange), sem alterar nada.
    verificar_atividade=False desliga a trava de atividade recente (uso manual, por sua conta e risco)."""
    alvo = df[df[COL_CLASSIFICACAO] == CLASSIFICACAO_ALVO]
    alvo = alvo[alvo[COL_TIPO].astype(str).str.lower().isin(TIPOS_ELEGIVEIS)]

    if len(alvo) > limite_max:
        raise RuntimeError(
            f"{len(alvo)} candidatas excedem o limite de segurança ({limite_max}). "
            "Revise o relatório manualmente ou aumente o limite conscientemente."
        )

    alvo = alvo.sort_values(COL_DIAS, ascending=False)
    # Sem a coluna Estagio (ou com valor diferente de CICLO COMPLETO), o padrão conservador é só bloquear
    pendentes = [(r[COL_EMAIL], r[COL_TIPO],
                  ESTAGIO_COMPLETO if r.get(COL_ESTAGIO) == ESTAGIO_COMPLETO else ESTAGIO_BLOQUEAR)
                 for _, r in alvo.iterrows()]

    resultados, alteradas, erros_seguidos = [], 0, 0
    # Processa em blocos; contas que não alteram nada (PULADA/ERRO) não gastam o lote
    while pendentes and (lote is None or alteradas < lote):
        n = len(pendentes) if lote is None else lote - alteradas
        chunk, pendentes = pendentes[:n], pendentes[n:]
        res, falha_exchange = _processar_chunk(token, chunk, dry_run, verificar_atividade)
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