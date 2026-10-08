from core.graph_auth import get_graph_token
from core.graph_relatorio_email import (
    get_email_activity_report, get_dados_entra, enviar_email_resumo, enviar_email_aviso,
)
from core.extracao_tipos_caixa import atualizar_tipos_caixa
from core.classificacao_contas import gerar_relatorio_inativos
from core.bloqueio_login import processar_bloqueios
from core.conversao_e_licenca import processar_fase2
from dotenv import load_dotenv, find_dotenv
from datetime import datetime
import os
import sys

# Raiz do projeto: os caminhos relativos (config/, relatorios/) funcionam de qualquer pasta
RAIZ = os.path.dirname(os.path.abspath(__file__))
os.chdir(RAIZ)

# O .env é o painel de controle: prevalece sobre variáveis já definidas no terminal (override=True),
# para que um valor esquecido na sessão não altere o modo de bloqueio sem você perceber.
CAMINHO_ENV = find_dotenv()
load_dotenv(CAMINHO_ENV, override=True)


def _env_bool(nome, padrao):
    valor = os.getenv(nome)
    if valor is None:
        return padrao
    return valor.strip().lower() in ("1", "true", "sim", "yes")


def _alertar(token, remetente, destinatario, assunto, mensagem):
    """Envia um e-mail curto de alerta. Nunca levanta exceção (já estamos tratando uma falha)."""
    if not (token and remetente and destinatario):
        return
    try:
        enviar_email_aviso(token, remetente, destinatario, assunto, mensagem)
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ Não foi possível enviar o e-mail de alerta: {e}")


def main():
    """Retorna 0 em caso de sucesso e 1 se a execução foi cancelada por erro (útil para o Agendador de Tarefas)."""
    print("="*50)
    print("Iniciando Automação de Contas Inativas - Otimizada")
    print("="*50)
    print(f"Configuração lida de: {CAMINHO_ENV or '(.env não encontrado)'}")

    EMAIL_REMETENTE = os.getenv("EMAIL_REMETENTE")
    EMAIL_DESTINATARIO = os.getenv("EMAIL_DESTINATARIO")
    token = None

    try:
        # Controle do bloqueio (.env). DRY_RUN é seguro por padrão: só bloqueia se DRY_RUN=false
        DRY_RUN = _env_bool("DRY_RUN", True)
        LIMITE_MAX = int(os.getenv("LIMITE_MAX", "30"))
        LOTE = int(os.getenv("LOTE", "0")) or None  # 0/vazio = sem lote
        FASE = os.getenv("FASE", "1").strip()  # 1 = só bloqueia (freio geral) | 2 = escalonado por estágio
        ATUALIZAR_TIPOS_CAIXA = _env_bool("ATUALIZAR_TIPOS_CAIXA", True)

        modo = "SIMULAÇÃO (dry-run)" if DRY_RUN else "REAL - contas serão bloqueadas"
        print(f"Modo de bloqueio: {modo} | fase={FASE} | limite_max={LIMITE_MAX} | lote={LOTE}")

        token = get_graph_token()
        print("✅ Autenticação com Azure realizada!")

        # Passo 0: atualiza os tipos de caixa no Exchange. Sem isso, não age (evita decidir com dados velhos).
        if ATUALIZAR_TIPOS_CAIXA:
            try:
                total_caixas = atualizar_tipos_caixa()
                print(f"📦 Tipos de caixa atualizados no Exchange: {total_caixas} caixas")
            except Exception as e:
                msg = ("A extração dos tipos de caixa falhou, então a execução foi cancelada para não agir "
                       f"com dados desatualizados. Detalhe: {e}")
                print(f"❌ {msg}")
                _alertar(token, EMAIL_REMETENTE, EMAIL_DESTINATARIO,
                         "Automação M365: execução cancelada (extração do Exchange falhou)", msg)
                return 1
        else:
            print("⚠️ ATUALIZAR_TIPOS_CAIXA=false: usando o CSV de tipos de caixa já existente.")

        # Agora só tem UMA chamada de API para o relatório (MUITO MAIS RÁPIDO!)
        lista_relatorio = get_email_activity_report(token)

        # Dados do Entra ID: data de criação (conta nova x antiga) e se o login já está bloqueado
        try:
            dados_entra = get_dados_entra(token)
            print(f"📅 Dados do Entra ID obtidos: {len(dados_entra)} contas")
        except Exception as e:
            dados_entra = {}
            print(f"⚠️ Não foi possível obter os dados do Entra ID ({e}). "
                  "Contas nunca acessadas ficam em revisão manual e contas já bloqueadas não serão filtradas.")

        df_resultado, caminho_csv = gerar_relatorio_inativos(lista_relatorio, dados_entra)

        # --- Bloqueio (fase 1) ou escalonado por estágio (fase 2) ---
        df_bloqueios, caminho_bloqueios, erro_bloqueio = None, None, None
        try:
            if FASE == "2":
                df_bloqueios = processar_fase2(
                    token, df_resultado,
                    dry_run=DRY_RUN, limite_max=LIMITE_MAX, lote=LOTE,
                )
            else:
                df_bloqueios = processar_bloqueios(
                    token, df_resultado,
                    dry_run=DRY_RUN, limite_max=LIMITE_MAX, lote=LOTE,
                )
            if not df_bloqueios.empty:
                caminho_bloqueios = f"relatorios/Bloqueios_{datetime.now():%Y%m%d_%H%M}.csv"
                os.makedirs("relatorios", exist_ok=True)
                df_bloqueios.to_csv(caminho_bloqueios, index=False, sep=';', encoding='utf-8-sig')
                print(f"📝 Resultado dos bloqueios salvo em: {caminho_bloqueios}")
        except Exception as e:
            # Falha no bloqueio não pode impedir o envio do relatório
            erro_bloqueio = str(e)
            print(f"⚠️ Bloqueio não executado: {erro_bloqueio}")

        # Envia e-mail passando o dataframe para montar o resumo HTML
        enviar_email_resumo(
            token, EMAIL_REMETENTE, EMAIL_DESTINATARIO, caminho_csv, df_resultado,
            df_bloqueios=df_bloqueios, caminho_bloqueios=caminho_bloqueios,
            erro_bloqueio=erro_bloqueio, dry_run=DRY_RUN,
        )
        return 0

    except Exception as e:
        print(f"\n❌ Erro crítico: {e}")
        _alertar(token, EMAIL_REMETENTE, EMAIL_DESTINATARIO,
                 "Automação M365: erro crítico na execução", str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())