# core/classificacao_contas.py — cruza atividade, licenças e tipo de caixa e classifica cada conta

import pandas as pd
from datetime import datetime
import os

# Licenças que não geram custo: não adianta "recuperar" e não devem contar como licença alocada.
# Confira no seu tenant se há outras (a coluna 'Licenças' do relatório mostra todas as que existem).
LICENCAS_SEM_CUSTO = {
    'MICROSOFT POWER AUTOMATE FREE',
    'MICROSOFT FABRIC (FREE)',
    'MICROSOFT BUSINESS CENTER',
    'MICROSOFT POWER APPS FOR DEVELOPER',
}

NUNCA_ACESSADA = 999  # valor usado quando não há Last Activity Date
DIAS_CONTA_RECENTE = 15  # conta criada há até N dias e sem acesso ainda é considerada recente
DIAS_LIMITE_USUARIO = 60        # UserMailbox com licença paga: candidata a bloqueio acima disso
DIAS_LIMITE_COMPARTILHADA = 90  # SharedMailbox com licença paga: candidata a bloqueio acima disso


def tem_licenca_paga(licencas):
    if not isinstance(licencas, str) or licencas == 'Sem Licença':
        return False
    itens = [i.strip().upper() for i in licencas.split(',')]
    return any(i not in LICENCAS_SEM_CUSTO for i in itens)


def gerar_relatorio_inativos(lista_relatorio_unificado, datas_criacao=None):
    print("\nProcessando Relatório Unificado e cruzando com o Exchange...")

    # 1. Tratamento do Relatório Único (Graph API)
    df_atividade = pd.DataFrame(lista_relatorio_unificado)
    df_atividade['Email'] = df_atividade.get('User Principal Name', '').str.lower()
    df_atividade['Nome'] = df_atividade.get('Display Name', '')
    # "Assigned Products" vem como string separada por "+"; trata também NaN
    df_atividade['Licenças'] = (
        df_atividade.get('Assigned Products', '')
        .fillna('')
        .replace('', 'Sem Licença')
        .str.replace('+', ', ', regex=False)
    )
    df_atividade['Ultima_Atividade'] = pd.to_datetime(df_atividade.get('Last Activity Date'), errors='coerce')

    df_atividade = df_atividade[['Email', 'Nome', 'Licenças', 'Ultima_Atividade']]

    # 2. Tratamento de Tipos de Caixa (PowerShell / Exchange)
    caminho_exchange = 'config/tipos_caixa_exchange.csv'
    if os.path.exists(caminho_exchange):
        df_exchange = pd.read_csv(caminho_exchange, sep=';')
        df_exchange['Email'] = df_exchange['PrimarySmtpAddress'].str.lower()
        df_exchange['Tipo_Caixa'] = df_exchange['RecipientTypeDetails']
        df_exchange = df_exchange[['Email', 'Tipo_Caixa']]
    else:
        raise Exception(f"Arquivo {caminho_exchange} não encontrado! Rode o script PowerShell primeiro.")

    # 3. Cruzamento
    df_final = pd.merge(df_atividade, df_exchange, on='Email', how='left')

    # 3.1 Data de criação da conta (Entra ID): ajuda a separar conta recém-criada de conta antiga nunca usada
    df_final['Data_Criacao'] = pd.to_datetime(
        df_final['Email'].map(datas_criacao or {}), errors='coerce', utc=True
    ).dt.tz_localize(None).dt.normalize()

    # 4. Whitelist
    caminho_whitelist = 'config/whitelist.csv'
    if os.path.exists(caminho_whitelist):
        df_whitelist = pd.read_csv(caminho_whitelist)
        lista_whitelist = df_whitelist['email'].str.lower().tolist()
    else:
        lista_whitelist = []

    # 5. Regras de Negócio
    hoje = pd.to_datetime(datetime.now().date())
    df_final['Dias_Inativa'] = (hoje - df_final['Ultima_Atividade']).dt.days
    df_final['Dias_Desde_Criacao'] = (hoje - df_final['Data_Criacao']).dt.days

    def classificar_conta(row):
        if row['Email'] in lista_whitelist:
            return 'WHITELIST'

        dias_inativa = row['Dias_Inativa'] if pd.notna(row['Dias_Inativa']) else NUNCA_ACESSADA
        tipo_caixa = str(row['Tipo_Caixa']).strip().lower()

        # Caixas de sistema
        if tipo_caixa in ['discoverymailbox', 'roommailbox', 'schedulingmailbox']:
            return 'IGNORAR'

        # Sem licença paga não há custo a recuperar (vale para qualquer tipo de caixa)
        if not tem_licenca_paga(row['Licenças']):
            return 'IGNORAR'

        # Tipo não identificado (não casou com o export do Exchange): nunca bloquear sozinho
        if tipo_caixa not in ('sharedmailbox', 'usermailbox'):
            return 'REVISAR (TIPO DESCONHECIDO)'

        limite = DIAS_LIMITE_COMPARTILHADA if tipo_caixa == 'sharedmailbox' else DIAS_LIMITE_USUARIO

        # Sem nenhuma atividade registrada: a data de criação separa a conta recém-criada (ainda não
        # deu tempo de usar) da conta antiga. A antiga continua em revisão manual, pois o relatório
        # de e-mail não mostra caixas que só recebem mensagens.
        if dias_inativa >= NUNCA_ACESSADA:
            criada = row['Data_Criacao']
            if pd.notna(criada) and (hoje - criada).days <= DIAS_CONTA_RECENTE:
                return 'CONTA RECENTE'
            return 'REVISAR (NUNCA ACESSADA)'

        return 'CANDIDATA A BLOQUEIO' if dias_inativa > limite else 'ATIVA'

    df_final['Classificacao'] = df_final.apply(classificar_conta, axis=1)

    # Limpeza
    qtd_ignoradas = len(df_final[df_final['Classificacao'] == 'IGNORAR'])
    print(f"🧹 Contas de sistema e contas sem licença paga ocultadas: {qtd_ignoradas}")
    df_final = df_final[df_final['Classificacao'] != 'IGNORAR'].copy()

    # Formatação
    df_final['Dias_Inativa'] = df_final['Dias_Inativa'].fillna(NUNCA_ACESSADA).astype(int)
    df_final['Ultima_Atividade'] = df_final['Ultima_Atividade'].dt.strftime('%d/%m/%Y').fillna('Nunca Acessada')
    df_final['Data_Criacao'] = df_final['Data_Criacao'].dt.strftime('%d/%m/%Y').fillna('Desconhecida')
    df_final['Dias_Desde_Criacao'] = df_final['Dias_Desde_Criacao'].astype('Int64')
    df_final['Na_Whitelist'] = df_final['Email'].apply(lambda x: 'Sim' if x in lista_whitelist else 'Não')
    df_final['Tipo_Caixa'] = df_final['Tipo_Caixa'].fillna('Desconhecido')

    df_final = df_final[['Email', 'Nome', 'Tipo_Caixa', 'Licenças', 'Data_Criacao', 'Dias_Desde_Criacao',
                         'Ultima_Atividade', 'Dias_Inativa', 'Na_Whitelist', 'Classificacao']]

    # 6. Exportação
    data_hoje = datetime.now().strftime("%Y%m%d_%H%M")
    caminho_exportacao = f'relatorios/Relatorio_Inativos_{data_hoje}.csv'
    os.makedirs('relatorios', exist_ok=True)
    df_final.to_csv(caminho_exportacao, index=False, sep=';', encoding='utf-8-sig')

    return df_final, caminho_exportacao
