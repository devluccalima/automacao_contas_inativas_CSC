"""ferramentas/processar_lista_contas.py — ciclo completo (bloquear → converter em compartilhada → remover licença)
em uma LISTA de contas que você já revisou.

Uso (de qualquer pasta; o .env é lido da raiz do projeto):
    python ferramentas/processar_lista_contas.py lista.txt                # simulação: só lê, não altera nada
    python ferramentas/processar_lista_contas.py lista.txt --real         # executa; pede para digitar a quantidade
    python ferramentas/processar_lista_contas.py lista.txt --real --bloco 5  # 5 contas por conexão com o Exchange
    python ferramentas/processar_lista_contas.py lista.txt --real --forcar   # ignora a trava de atividade recente

TRAVA: antes de agir, o Exchange é consultado (somente leitura). Conta com logon ou interação do usuário no
Exchange nos últimos 14 dias NÃO é alterada (PULADA), desde que o login ainda esteja ativo: com o login já bloqueado a
atividade vem de delegados ou processos e não impede converter nem tirar a licença, mesmo que o relatório de uso, que chega com ~2 dias de atraso, a mostre inativa.

As contas são processadas em BLOCOS (padrão: 8). Cada bloco leva de 3 a 5 minutos (20 a 30 s por conta) e o resultado é
salvo ao fim de cada um. Se você interromper (Ctrl+C) ou algo falhar, rode o mesmo comando de novo: as contas que já
ficaram sem licença são puladas automaticamente.

O arquivo pode ter um e-mail por linha ou linhas copiadas do relatório: vale o PRIMEIRO e-mail de cada linha.
Contas que estão na whitelist são ignoradas. No servidor, rode como a conta de serviço (o certificado do Exchange
está no perfil dela) e fora do horário da tarefa agendada, para as duas execuções não se sobreporem.
"""
import argparse
import os
import re
import sys
from datetime import datetime

import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv(override=True)  # o .env prevalece sobre variáveis já definidas no terminal

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)

from core.graph_auth import get_graph_token
from core.bloqueio_login import GRAPH, _headers
from core.conversao_e_licenca import processar_fase2

VARIAVEIS = ["AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET",
             "EXO_APP_ID", "EXO_THUMBPRINT", "EXO_ORGANIZATION"]
PADRAO_EMAIL = re.compile(r"[A-Za-z0-9._%+'\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def ler_emails(caminho):
    """Primeiro e-mail de cada linha, em minúsculas, sem repetição e na ordem do arquivo."""
    vistos, emails = set(), []
    with open(caminho, encoding="utf-8-sig", errors="replace") as f:
        for linha in f:
            achado = PADRAO_EMAIL.search(linha)
            if achado:
                e = achado.group(0).lower()
                if e not in vistos:
                    vistos.add(e)
                    emails.append(e)
    return emails


def ler_whitelist():
    caminho = os.path.join(RAIZ, "config", "whitelist.csv")
    if not os.path.exists(caminho):
        return set()
    df = pd.read_csv(caminho)
    return {str(e).strip().lower() for e in df["email"].dropna()}


def estado_graph(token, email):
    r = requests.get(
        f"{GRAPH}/users/{email}", headers=_headers(token), timeout=30,
        params={"$select": "displayName,accountEnabled,onPremisesSyncEnabled,licenseAssignmentStates"},
    )
    if r.status_code == 404:
        return None
    r.raise_for_status()
    u = r.json()
    estados = u.get("licenseAssignmentStates", [])
    return {
        "nome": u.get("displayName"),
        "login_ativo": u.get("accountEnabled"),
        "sincronizada_ad": bool(u.get("onPremisesSyncEnabled")),
        "diretas": len({e["skuId"] for e in estados if not e.get("assignedByGroup")}),
        "de_grupo": len({e["skuId"] for e in estados if e.get("assignedByGroup")}),
    }


def main():
    ap = argparse.ArgumentParser(description="Ciclo completo em uma lista de contas já revisadas.")
    ap.add_argument("arquivo", help="arquivo de texto com os e-mails (um por linha ou linhas copiadas do relatório)")
    ap.add_argument("--real", action="store_true", help="executa de verdade (padrão: simulação)")
    ap.add_argument("--bloco", type=int, default=8,
                    help="quantas contas por conexão com o Exchange (padrão: 8; o resultado é salvo a cada bloco)")
    ap.add_argument("--forcar", action="store_true",
                    help="ignora a trava de atividade recente no Exchange (use só se você conferiu a conta)")
    ap.add_argument("--tipo", default="UserMailbox", choices=["UserMailbox", "SharedMailbox"],
                    help="tipo atual das caixas da lista (padrão: UserMailbox; o Exchange confirma o tipo real)")
    args = ap.parse_args()

    faltando = [v for v in VARIAVEIS if not os.getenv(v)]
    if faltando:
        sys.exit(f"Variáveis ausentes no .env: {', '.join(faltando)}")
    if not os.path.exists(args.arquivo):
        sys.exit(f"Arquivo não encontrado: {args.arquivo}")

    emails = ler_emails(args.arquivo)
    if not emails:
        sys.exit("Nenhum e-mail encontrado no arquivo.")

    whitelist = ler_whitelist()
    na_whitelist = [e for e in emails if e in whitelist]
    emails = [e for e in emails if e not in whitelist]

    print("=" * 70)
    print(f"Lista de contas — modo: {'REAL' if args.real else 'SIMULAÇÃO (nada será alterado)'}")
    print("=" * 70)
    print(f"{len(emails)} contas na lista" + (f" (mais {len(na_whitelist)} ignorada(s) por estar(em) na whitelist)" if na_whitelist else ""))
    for e in na_whitelist:
        print(f"   ignorada (whitelist): {e}")

    # Conferência rápida no Entra ID antes de qualquer ação (só leitura)
    token = get_graph_token()
    aptas, problemas, concluidas = [], [], []
    print("\n[antes] Entra ID: login | licenças diretas / de grupo | sincronizada com o AD")
    for e in emails:
        try:
            g = estado_graph(token, e)
        except requests.HTTPError as ex:
            problemas.append((e, f"erro ao consultar ({ex.response.status_code})"))
            continue
        if g is None:
            problemas.append((e, "não encontrada no Entra ID"))
            continue
        print(f"   {e:<48} login={'ativo' if g['login_ativo'] else 'bloqueado':<9} "
              f"lic={g['diretas']}/{g['de_grupo']}  AD={'sim' if g['sincronizada_ad'] else 'não'}")
        if g["diretas"] == 0 and g["de_grupo"] == 0:
            # A licença só é removida depois da conversão confirmada: sem licença, o ciclo já foi concluído
            print("      ✔ já está sem licença: ciclo concluído, será ignorada")
            concluidas.append(e)
            continue
        if g["sincronizada_ad"]:
            print("      ⚠ conta sincronizada com o AD: o login não pode ser alterado por aqui (será PULADA)")
        if g["de_grupo"]:
            print("      ⚠ licença herdada de grupo: a caixa será convertida, mas a licença exige tirar a conta do grupo")
        aptas.append(e)
    for e, motivo in problemas:
        print(f"   ignorada: {e} — {motivo}")

    if args.forcar:
        print("\n⚠ --forcar: a trava de atividade recente no Exchange está DESLIGADA.")
    print(f"\nPara processar: {len(aptas)} | já concluídas: {len(concluidas)} | ignoradas: {len(problemas)}")
    if not aptas:
        sys.exit("Nenhuma conta apta para processar.")

    if args.real:
        print(f"\nATENÇÃO: isto vai bloquear o login, converter em compartilhada e remover as licenças diretas de {len(aptas)} contas.")
        if input(f"Para confirmar, digite a quantidade de contas ({len(aptas)}): ").strip() != str(len(aptas)):
            sys.exit("Confirmação diferente da quantidade de contas. Nada foi feito.")

    blocos = [aptas[k:k + args.bloco] for k in range(0, len(aptas), args.bloco)]
    os.makedirs(os.path.join(RAIZ, "relatorios"), exist_ok=True)
    caminho_csv = os.path.join(RAIZ, "relatorios", f"Bloqueios_lista_{datetime.now():%Y%m%d_%H%M}.csv")
    resultados, interrompido = [], False

    try:
        for n, bloco in enumerate(blocos, 1):
            print(f"\n[bloco {n}/{len(blocos)}] {len(bloco)} contas (20 a 30 s por conta; não feche a janela)...", flush=True)
            df = pd.DataFrame([{
                "Email": e, "Tipo_Caixa": args.tipo, "Dias_Inativa": 120,
                "Classificacao": "CANDIDATA A BLOQUEIO", "Estagio": "CICLO COMPLETO",
            } for e in bloco])
            res = processar_fase2(token, df, dry_run=not args.real, limite_max=len(df),
                                  verificar_atividade=not args.forcar)
            resultados.append(res)
            pd.concat(resultados, ignore_index=True).to_csv(caminho_csv, index=False, sep=";", encoding="utf-8-sig")
            resumo = ", ".join(f"{k}: {v}" for k, v in res["Resultado"].value_counts().items())
            print(f"[bloco {n}/{len(blocos)}] concluído — {resumo}", flush=True)
            # Falha do script do Exchange afeta todas as contas do bloco: não adianta insistir nos próximos
            if not res.empty and res["Detalhe"].astype(str).str.startswith("[exchange]").all():
                print("\n⚠ O script do Exchange falhou neste bloco; os próximos não foram tentados.")
                interrompido = True
                break
    except KeyboardInterrupt:
        print("\n⚠ Interrompido pelo usuário. As contas dos blocos já concluídos foram salvas; as do bloco em andamento "
              "podem estar parcialmente processadas (sem risco: rode de novo para continuar).")
        interrompido = True

    if not resultados:
        sys.exit("Nenhum bloco foi concluído.")
    res = pd.concat(resultados, ignore_index=True)

    print("\n--- Resultado por situação ---")
    for resultado, n in res["Resultado"].value_counts().items():
        print(f"   {resultado:<18} {n}")
    nao_ok = res[~res["Resultado"].isin(["LICENCA_REMOVIDA", "SIMULADO"])]
    if not nao_ok.empty:
        print("\n--- Contas que precisam de atenção ---")
        for _, r in nao_ok.iterrows():
            print(f"   {r['Email']}: {r['Resultado']} — {r['Detalhe'] or '-'}")
    if interrompido and len(res) < len(aptas):
        print(f"\nFicaram {len(aptas) - len(res)} contas sem processar. Rode o mesmo comando para continuar.")
    print(f"\nResultado salvo em: {caminho_csv}")


if __name__ == "__main__":
    main()