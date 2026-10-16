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
# True: conta nunca acessada é tratada pelas mesmas regras, contando os dias desde a CRIAÇÃO da conta.
# False: conta nunca acessada fica sempre em revisão manual.
TRATAR_NUNCA_ACESSADAS = True
# Escalonamento por dias de inatividade (contas com licença paga):
DIAS_BLOQUEIO_USUARIO = 35         # UserMailbox acima disso: candidata, estágio BLOQUEAR (só bloqueia o login)
DIAS_CONVERSAO_USUARIO = 55        # UserMailbox acima disso: estágio CICLO COMPLETO (bloquear -> converter -> remover licença)
DIAS_CONVERSAO_COMPARTILHADA = 55  # SharedMailbox acima disso: candidata direto no CICLO COMPLETO (já é compartilhada)

# True: o CSV traz só o que exige atenção (candidatas e revisão manual). False: traz todas as contas.
CSV_SOMENTE_PENDENCIAS = True


def tem_licenca_paga(licencas):
    if not isinstance(licencas, str) or licencas == 'Sem Licença':
        return False
    itens = [i.strip().upper() for i in licencas.split(',')]
    return any(i not in LICENCAS_SEM_CUSTO for i in itens)


def gerar_relatorio_inativos(lista_relatorio_unificado, dados_entra=None):
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

    # Data até a qual o relatório de uso tem dados (Report Refresh Date). O relatório chega com atraso e já ficou
    # parado por dias: a data dos dados é que diz a idade real da informação.
    data_dos_dados = None
    if 'Report Refresh Date' in df_atividade.columns:
        maxima = pd.to_datetime(df_atividade['Report Refresh Date'], errors='coerce').max()
        if pd.notna(maxima):
            data_dos_dados = maxima.normalize()
    if data_dos_dados is not None:
        print(f"📅 Dados do relatório de uso até {data_dos_dados:%d/%m/%Y}")
    else:
        print("⚠️ O relatório de uso não informou a data dos dados (Report Refresh Date)")

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

    # 3.1 Dados do Entra ID: data de criação (separa conta recém-criada de conta antiga nunca usada)
    #     e se o login está ativo (evita trazer de novo contas que já foram bloqueadas)
    dados = dados_entra or {}
    df_final['Data_Criacao'] = pd.to_datetime(
        df_final['Email'].map(lambda e: (dados.get(e) or {}).get('criada_em')), errors='coerce', utc=True
    ).dt.tz_localize(None).dt.normalize()
    df_final['Login_Ativo'] = df_final['Email'].map(lambda e: (dados.get(e) or {}).get('login_ativo'))
    df_final['Licencas_Entra'] = df_final['Email'].map(lambda e: (dados.get(e) or {}).get('n_licencas'))

    # 4. Whitelist
    caminho_whitelist = 'config/whitelist.csv'
    if os.path.exists(caminho_whitelist):
        df_whitelist = pd.read_csv(caminho_whitelist)
        lista_whitelist = df_whitelist['email'].str.lower().tolist()
    else:
        lista_whitelist = []

    # 5. Regras de Negócio
    # A inatividade é contada a partir da data dos DADOS (e não de "hoje"): com o relatório atrasado, contar a partir
    # de hoje faria toda conta parecer mais inativa do que o dado sustenta.
    hoje = data_dos_dados if data_dos_dados is not None else pd.to_datetime(datetime.now().date())
    df_final['Dias_Inativa'] = (hoje - df_final['Ultima_Atividade']).dt.days
    df_final['Dias_Desde_Criacao'] = (hoje - df_final['Data_Criacao']).dt.days

    # Conta nunca acessada: a inatividade conta desde a criação (quando a data de criação é conhecida)
    if TRATAR_NUNCA_ACESSADAS:
        sem_atividade = df_final['Ultima_Atividade'].isna() & df_final['Dias_Desde_Criacao'].notna()
        df_final.loc[sem_atividade, 'Dias_Inativa'] = df_final.loc[sem_atividade, 'Dias_Desde_Criacao']

    def classificar_conta(row):
        if row['Email'] in lista_whitelist:
            return 'WHITELIST'

        dias_inativa = row['Dias_Inativa'] if pd.notna(row['Dias_Inativa']) else NUNCA_ACESSADA
        tipo_caixa = str(row['Tipo_Caixa']).strip().lower()

        # Caixas de sistema
        if tipo_caixa in ['discoverymailbox', 'roommailbox', 'schedulingmailbox']:
            return 'IGNORAR'

        # Sem licença paga não há custo a recuperar (vale para qualquer tipo de caixa)
        # Licenças AO VIVO (Entra ID): o relatório de uso mostra as licenças com ~2 dias de atraso e ainda "enxerga" a que
        # acabamos de remover. Sem nenhuma licença agora, não há nada a recuperar.
        if pd.notna(row['Licencas_Entra']) and row['Licencas_Entra'] == 0:
            return 'IGNORAR'

        if not tem_licenca_paga(row['Licenças']):
            return 'IGNORAR'

        # Tipo não identificado (não casou com o export do Exchange): nunca bloquear sozinho
        if tipo_caixa not in ('sharedmailbox', 'usermailbox'):
            return 'REVISAR (TIPO DESCONHECIDO)'

        limite = DIAS_CONVERSAO_COMPARTILHADA if tipo_caixa == 'sharedmailbox' else DIAS_BLOQUEIO_USUARIO

        # Nunca acessada: sem data de criação não dá para julgar (revisão manual). Com ela, a idade da conta
        # vale como dias de inatividade e segue os mesmos limites; dentro do prazo, é conta recente.
        if pd.isna(row['Ultima_Atividade']):
            if not TRATAR_NUNCA_ACESSADAS or pd.isna(row['Data_Criacao']):
                return 'REVISAR (NUNCA ACESSADA)'
            return 'CANDIDATA A BLOQUEIO' if dias_inativa > limite else 'CONTA RECENTE'

        return 'CANDIDATA A BLOQUEIO' if dias_inativa > limite else 'ATIVA'

    df_final['Classificacao'] = df_final.apply(classificar_conta, axis=1)

    # Estágio de cada candidata: só bloquear o login, ou ciclo completo (bloquear -> converter -> remover licença)
    def definir_estagio(row):
        if row['Classificacao'] != 'CANDIDATA A BLOQUEIO':
            return ''
        compartilhada = str(row['Tipo_Caixa']).strip().lower() == 'sharedmailbox'
        limite_completo = DIAS_CONVERSAO_COMPARTILHADA if compartilhada else DIAS_CONVERSAO_USUARIO
        return 'CICLO COMPLETO' if row['Dias_Inativa'] > limite_completo else 'BLOQUEAR'

    df_final['Estagio'] = df_final.apply(definir_estagio, axis=1)

    # Já bloqueada e ainda dentro do prazo de conversão: não há nada a fazer agora (não é pendência)
    ja_bloqueada = ((df_final['Classificacao'] == 'CANDIDATA A BLOQUEIO')
                    & (df_final['Estagio'] == 'BLOQUEAR')
                    & df_final['Login_Ativo'].eq(False))
    df_final.loc[ja_bloqueada, 'Classificacao'] = 'BLOQUEADA (AGUARDANDO CONVERSÃO)'
    df_final.loc[ja_bloqueada, 'Estagio'] = ''

    # Limpeza
    qtd_ignoradas = len(df_final[df_final['Classificacao'] == 'IGNORAR'])
    sem_licenca_agora = int(((df_final['Classificacao'] == 'IGNORAR') & (df_final['Licencas_Entra'] == 0)).sum())
    print(f"🧹 Contas de sistema e contas sem licença paga ocultadas: {qtd_ignoradas} "
          f"(sem nenhuma licença no Entra ID agora: {sem_licenca_agora})")
    df_final = df_final[df_final['Classificacao'] != 'IGNORAR'].copy()

    # Formatação
    df_final['Dias_Inativa'] = df_final['Dias_Inativa'].fillna(NUNCA_ACESSADA).astype(int)
    df_final['Ultima_Atividade'] = df_final['Ultima_Atividade'].dt.strftime('%d/%m/%Y').fillna('Nunca Acessada')
    df_final['Data_Criacao'] = df_final['Data_Criacao'].dt.strftime('%d/%m/%Y').fillna('Desconhecida')
    df_final['Dias_Desde_Criacao'] = df_final['Dias_Desde_Criacao'].astype('Int64')
    df_final['Na_Whitelist'] = df_final['Email'].apply(lambda x: 'Sim' if x in lista_whitelist else 'Não')
    df_final['Login_Ativo'] = df_final['Login_Ativo'].map({True: 'Sim', False: 'Não'}).fillna('Desconhecido')
    df_final['Tipo_Caixa'] = df_final['Tipo_Caixa'].fillna('Desconhecido')

    df_final = df_final[['Email', 'Nome', 'Tipo_Caixa', 'Licenças', 'Data_Criacao', 'Dias_Desde_Criacao',
                         'Ultima_Atividade', 'Dias_Inativa', 'Login_Ativo', 'Na_Whitelist', 'Classificacao', 'Estagio']]

    # 6. Exportação: por padrão o CSV traz só o que exige atenção; o resto aparece como contagem no e-mail
    if CSV_SOMENTE_PENDENCIAS:
        df_csv = df_final[df_final['Classificacao'].str.match(r'(CANDIDATA|REVISAR)')]
    else:
        df_csv = df_final

    caminho_exportacao = None  # sem pendências, nenhum CSV é gerado
    if not df_csv.empty:
        data_hoje = datetime.now().strftime("%Y%m%d_%H%M")
        caminho_exportacao = f'relatorios/Relatorio_Inativos_{data_hoje}.csv'
        os.makedirs('relatorios', exist_ok=True)
        df_csv.sort_values('Dias_Inativa', ascending=False).to_csv(
            caminho_exportacao, index=False, sep=';', encoding='utf-8-sig')

    df_final.attrs['data_dos_dados'] = data_dos_dados  # lido pelo main.py (trava de dados velhos e e-mail)
    return df_final, caminho_exportacao