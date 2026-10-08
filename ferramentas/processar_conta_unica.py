"""ferramentas/processar_conta_unica.py — roda o ciclo completo da fase 2 em UMA conta específica:
bloquear login → converter em compartilhada → remover licença.

Útil para testar em conta descartável ou para tratar manualmente uma conta (por exemplo, uma exceção da whitelist).

Uso (de qualquer pasta; o .env é lido da raiz do projeto):
    python ferramentas/processar_conta_unica.py conta@dominio.com          # simulação: só lê, não altera nada
    python ferramentas/processar_conta_unica.py conta@dominio.com --real   # executa de verdade (pede confirmação)

Atenção: o script age SOMENTE na conta informada e ignora a classificação e a whitelist. Confira o e-mail antes de confirmar.
"""
import argparse
import os
import sys
import time

import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv(override=True)  # o .env prevalece sobre variáveis já definidas no terminal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # raiz do projeto

from core.graph_auth import get_graph_token
from core.bloqueio_login import GRAPH, _headers
from core.conversao_e_licenca import processar_fase2, _rodar_powershell

VARIAVEIS = ["AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET",
             "EXO_APP_ID", "EXO_THUMBPRINT", "EXO_ORGANIZATION"]


def estado_graph(token, email):
    r = requests.get(
        f"{GRAPH}/users/{email}", headers=_headers(token), timeout=30,
        params={"$select": "displayName,accountEnabled,createdDateTime,"
                           "onPremisesSyncEnabled,licenseAssignmentStates"},
    )
    r.raise_for_status()
    u = r.json()
    estados = u.get("licenseAssignmentStates", [])
    return {
        "nome": u.get("displayName"),
        "login_ativo": u.get("accountEnabled"),
        "criada_em": (u.get("createdDateTime") or "")[:10],
        "sincronizada_ad": u.get("onPremisesSyncEnabled"),
        "licencas_diretas": sorted({e["skuId"] for e in estados if not e.get("assignedByGroup")}),
        "licencas_de_grupo": sorted({e["skuId"] for e in estados if e.get("assignedByGroup")}),
    }


def estado_graph_aguardando(token, email, tentativas=6, espera=5):
    """Relê o estado até as licenças diretas sumirem (o Entra ID pode demorar alguns segundos para refletir)."""
    g = estado_graph(token, email)
    for _ in range(tentativas - 1):
        if not g["licencas_diretas"]:
            break
        print("   (licença ainda aparece; aguardando o Entra ID atualizar...)")
        time.sleep(espera)
        g = estado_graph(token, email)
    return g


def mostrar_graph(rotulo, g):
    print(f"[{rotulo}] Entra ID: login ativo={g['login_ativo']} | licenças diretas={len(g['licencas_diretas'])} "
          f"| herdadas de grupo={len(g['licencas_de_grupo'])} | criada em {g['criada_em']} "
          f"| sincronizada com AD local={bool(g['sincronizada_ad'])}")


def mostrar_exchange(rotulo, email):
    d = _rodar_powershell([email], somente_verificar=True).get(email)
    if d is None or d.get("Erro"):
        print(f"[{rotulo}] Exchange: erro: {(d or {}).get('Erro', 'sem retorno do script')}")
        return
    pode = "SIM" if d.get("PodeRemoverLicenca") else f"NÃO ({d.get('Motivos')})"
    print(f"[{rotulo}] Exchange: tipo={d.get('TipoInicial')} | licença pode ser removida com segurança: {pode}")


def main():
    ap = argparse.ArgumentParser(description="Ciclo completo da fase 2 (bloquear → converter → remover licença) em uma única conta.")
    ap.add_argument("email", help="e-mail (UPN) da conta")
    ap.add_argument("--real", action="store_true", help="executa de verdade (padrão: simulação)")
    ap.add_argument("--tipo", default="UserMailbox", choices=["UserMailbox", "SharedMailbox"],
                    help="tipo atual da caixa (padrão: UserMailbox)")
    args = ap.parse_args()
    email = args.email.strip().lower()

    faltando = [v for v in VARIAVEIS if not os.getenv(v)]
    if faltando:
        sys.exit(f"Variáveis ausentes no .env: {', '.join(faltando)}")

    print("=" * 60)
    print(f"Fase 2 em conta única — {email} — modo: {'REAL' if args.real else 'SIMULAÇÃO (nada será alterado)'}")
    print("=" * 60)

    token = get_graph_token()
    try:
        antes = estado_graph(token, email)
    except requests.HTTPError as e:
        sys.exit(f"Não foi possível ler a conta no Entra ID: {e.response.status_code} {e.response.text[:200]}")

    print(f"Conta encontrada: {antes['nome']}")
    mostrar_graph("antes", antes)
    mostrar_exchange("antes", email)

    if args.real:
        print("\nATENÇÃO: isto vai bloquear o login, converter a caixa em compartilhada e remover as licenças diretas.")
        print(f"Conta: {antes['nome']} <{email}>")
        if input("Para confirmar, digite o e-mail da conta: ").strip().lower() != email:
            sys.exit("Confirmação diferente do e-mail informado. Nada foi feito.")

    # Linha fictícia: garante que a conta passe pelos filtros e processa SOMENTE ela
    df = pd.DataFrame([{"Email": email, "Tipo_Caixa": args.tipo, "Dias_Inativa": 120,
                        "Classificacao": "CANDIDATA A BLOQUEIO", "Estagio": "CICLO COMPLETO"}])
    print("\nExecutando...")
    res = processar_fase2(token, df, dry_run=not args.real, limite_max=1, lote=1)

    print("\n--- Resultado ---")
    if res.empty:
        print("Nenhum resultado (a conta foi filtrada).")
    for _, r in res.iterrows():
        print(f"Resultado: {r['Resultado']}")
        print(f"Ação:      {r['Acao']}")
        print(f"Detalhe:   {r['Detalhe'] or '-'}")

    if args.real:
        print("\n--- Estado depois ---")
        mostrar_graph("depois", estado_graph_aguardando(token, email))
        mostrar_exchange("depois", email)
        print("\nEsperado: login ativo=False, licenças diretas=0, tipo=SharedMailbox.")
        print("\nPara desfazer:")
        print("  1) Entra ID: ativar 'Account enabled' e reatribuir a licença da conta.")
        print(f"  2) Exchange: Set-Mailbox -Identity {email} -Type Regular")


if __name__ == "__main__":
    main()
