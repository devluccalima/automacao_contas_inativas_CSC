from core.graph_auth import get_graph_token
from core.graph_relatorio_email import get_email_activity_report, get_datas_criacao, enviar_email_resumo
from core.classificacao_contas import gerar_relatorio_inativos
from core.bloqueio_login import processar_bloqueios
from core.conversao_e_licenca import processar_fase2
from dotenv import load_dotenv
from datetime import datetime
import os

load_dotenv()  # Carrega variáveis de ambiente do arquivo .env


def _env_bool(nome, padrao):
    valor = os.getenv(nome)
    if valor is None:
        return padrao
    return valor.strip().lower() in ("1", "true", "sim", "yes")


def main():
    print("="*50)
    print("Iniciando Automação de Contas Inativas - Otimizada")
    print("="*50)
    
    try:
        
        EMAIL_REMETENTE = os.getenv("EMAIL_REMETENTE")
        EMAIL_DESTINATARIO = os.getenv("EMAIL_DESTINATARIO")

        # Controle do bloqueio (.env). DRY_RUN é seguro por padrão: só bloqueia se DRY_RUN=false
        DRY_RUN = _env_bool("DRY_RUN", True)
        LIMITE_MAX = int(os.getenv("LIMITE_MAX", "30"))
        LOTE = int(os.getenv("LOTE", "0")) or None  # 0/vazio = sem lote
        FASE = os.getenv("FASE", "1").strip()  # 1 = só bloquear login | 2 = bloquear → converter → remover licença

        modo = "SIMULAÇÃO (dry-run)" if DRY_RUN else "REAL - contas serão bloqueadas"
        print(f"Modo de bloqueio: {modo} | fase={FASE} | limite_max={LIMITE_MAX} | lote={LOTE}")

        token = get_graph_token()
        print("✅ Autenticação com Azure realizada!")
        
        # Agora só tem UMA chamada de API (MUITO MAIS RÁPIDO!)
        lista_relatorio = get_email_activity_report(token)
        
        # Datas de criação das contas (para separar conta recém-criada de conta antiga nunca usada)
        try:
            datas_criacao = get_datas_criacao(token)
            print(f"📅 Datas de criação obtidas: {len(datas_criacao)} contas")
        except Exception as e:
            datas_criacao = {}
            print(f"⚠️ Não foi possível obter as datas de criação ({e}). Contas nunca acessadas ficam em revisão manual.")

        df_resultado, caminho_csv = gerar_relatorio_inativos(lista_relatorio, datas_criacao)

        # --- Fase 1: bloquear o login das candidatas ---
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
        
    except Exception as e:
        print(f"\n❌ Erro crítico: {e}")

if __name__ == "__main__":
    main()
