# Automação de Contas Inativas — Microsoft 365

Audita caixas de e-mail do M365, identifica contas inativas que ainda consomem licença e, conforme a fase
configurada, bloqueia o login e recupera a licença. Cada execução gera um CSV e envia um e-mail com o resumo.

## Estrutura

```
automacao_contas_inativas/
├── main.py                          Orquestrador: roda tudo na ordem
├── requirements.txt                 Dependências Python
├── .env                             Segredos e controles (NÃO versionar; modelo em .env.example)
├── config/
│   ├── tipos_caixa_exchange.csv     Gerado por scripts/extrai_tipos_caixa.ps1
│   └── whitelist.csv                Coluna "email": contas que nunca são tocadas
├── scripts/
│   ├── extrai_tipos_caixa.ps1       Exporta o tipo de cada caixa (UserMailbox, SharedMailbox...)
│   ├── converter_para_compartilhada.ps1   Fase 2: converte caixa e verifica se a licença pode sair
│   └── executar_automacao.ps1       Usado pelo Agendador de Tarefas: roda o main.py e grava o log em logs/
├── ferramentas/
│   └── processar_conta_unica.py     Roda a fase 2 completa em UMA conta específica (simulação ou real)
├── core/
│   ├── graph_auth.py                Autenticação no Microsoft Graph
│   ├── extracao_tipos_caixa.py      Roda extrai_tipos_caixa.ps1 antes de cada execução e valida o CSV
│   ├── graph_relatorio_email.py     Baixa o relatório D180, busca dados do Entra ID, envia o e-mail HTML
│   ├── classificacao_contas.py      Cruza os dados e classifica cada conta
│   ├── bloqueio_login.py            Fase 1: bloqueia o login (Graph)
│   └── conversao_e_licenca.py       Fase 2: bloquear → converter em compartilhada → remover licença
├── relatorios/                      Saída: Relatorio_Inativos_*.csv (só pendências) e Bloqueios_*.csv
└── logs/                            Saída do agendamento (execucao_AAAAMMDD_HHMM.log, 90 dias)
```

## Ordem de execução

```powershell
python main.py
```

O `main.py` já faz tudo na ordem, de qualquer pasta: (0) atualiza `config\tipos_caixa_exchange.csv` no Exchange
(se falhar, **cancela a execução e avisa por e-mail**, sem agir com dados velhos), (1) baixa o relatório de atividade
e os dados do Entra ID, (2) classifica, (3) simula ou executa o bloqueio, (4) envia o e-mail. Sem nenhuma candidata,
o e-mail informa "0 caixas inativas para efetuar a automação". O código de saída é 1 quando a execução é cancelada.

## Agendamento semanal (Agendador de Tarefas do Windows)

1. Valide primeiro a tarefa com `DRY_RUN=true` no `.env`; só depois mude para `false`.
2. No Agendador de Tarefas: **Criar Tarefa** (não "Criar Tarefa Básica").
   - **Geral:** nome `Automacao Contas Inativas M365`; usuário que tem o certificado do Exchange instalado;
     "Executar estando o usuário conectado ou não".
   - **Disparadores:** semanal, no dia e horário desejados.
   - **Ações:** programa `powershell.exe`; argumentos
     `-NoProfile -ExecutionPolicy Bypass -File "C:\caminho\automacao_contas_inativas\scripts\executar_automacao.ps1"`.
   - **Condições:** desmarque "Iniciar somente se o computador estiver ligado na energia da rede" se for notebook.
   - **Configurações:** marque "Executar a tarefa o mais cedo possível após uma inicialização agendada perdida"
     e "Se a tarefa já estiver em execução: não iniciar uma nova instância".
3. Teste com **Executar** (botão direito) e confira `logs\` e o e-mail.
4. Para um primeiro período de operação autônoma, deixe `LOTE` baixo (por exemplo `10`) no `.env`.

## Processar uma conta específica

Para testar em conta descartável ou tratar uma conta à mão (ignora classificação e whitelist):

```powershell
python ferramentas\processar_conta_unica.py conta@dominio.com          # simulação, só lê
python ferramentas\processar_conta_unica.py conta@dominio.com --real   # executa; pede para digitar o e-mail
```

## Controles no .env

| Variável | Efeito |
|---|---|
| `DRY_RUN` | `true` (padrão) só simula; só altera contas com `false` |
| `FASE` | `1` só bloqueia o login de todas as candidatas (freio geral); `2` escalonado: estágio `BLOQUEAR` só bloqueia, estágio `CICLO COMPLETO` bloqueia → converte em compartilhada → remove a licença |
| `LIMITE_MAX` | Se houver mais candidatas que isso, aborta o bloqueio |
| `LOTE` | Quantas contas alterar por execução (`0` = sem limite) |
| `EXO_APP_ID`, `EXO_THUMBPRINT`, `EXO_ORGANIZATION` | Autenticação por certificado no Exchange Online |
| `ATUALIZAR_TIPOS_CAIXA` | `true` (padrão) roda a extração do Exchange antes de cada execução; `false` usa o CSV existente |
| `POWERSHELL_EXE` | Opcional. `pwsh` se o módulo ExchangeOnlineManagement estiver no PowerShell 7 |

## Regras de classificação (`core/classificacao_contas.py`)

| Situação | Resultado |
|---|---|
| E-mail na whitelist | `WHITELIST` |
| Caixa de sistema (Room, Discovery, Scheduling) ou sem licença paga | ocultada |
| Tipo de caixa não identificado | `REVISAR (TIPO DESCONHECIDO)` |
| `UserMailbox` com licença paga, inativa há mais de `DIAS_BLOQUEIO_USUARIO` (20) dias | `CANDIDATA A BLOQUEIO`, estágio `BLOQUEAR` (só bloqueia o login) |
| `UserMailbox` inativa há mais de `DIAS_CONVERSAO_USUARIO` (45) dias | `CANDIDATA A BLOQUEIO`, estágio `CICLO COMPLETO` (bloquear → converter → remover licença) |
| `SharedMailbox` com licença paga, inativa há mais de `DIAS_CONVERSAO_COMPARTILHADA` (45) dias | `CANDIDATA A BLOQUEIO`, estágio `CICLO COMPLETO` |
| Sem atividade e criada há até `DIAS_CONTA_RECENTE` (15) dias | `CONTA RECENTE` |
| Sem atividade e criada há mais tempo (ou sem data de criação) | `REVISAR (NUNCA ACESSADA)` |
| Candidata no estágio `BLOQUEAR` cujo login **já está bloqueado** | `BLOQUEADA (AGUARDANDO CONVERSÃO)` (não é pendência) |
| Demais casos | `ATIVA` |

Só `CANDIDATA A BLOQUEIO` é processada. Contas `REVISAR` nunca são bloqueadas automaticamente.

**O que vai no CSV:** por padrão (`CSV_SOMENTE_PENDENCIAS = True`), só candidatas e contas para revisão manual. Contas
ativas, na whitelist, recentes ou já bloqueadas aguardando conversão aparecem como contagem no e-mail. Sem pendências,
nenhum CSV é gerado. Candidatas com **mais de 45 dias** continuam como pendência mesmo já bloqueadas, porque ainda falta
converter e remover a licença.

## Permissões necessárias

- **Graph (aplicativo, com consentimento do admin):** `Mail.Send`, `User.ReadWrite.All` (inclui leitura de usuários e datas de criação).
- **Exchange Online:** `Exchange.ManageAsApp` e uma função que permita `Set-Mailbox` (ex.: Exchange Administrator) para o service principal.
- Contas com função administrativa no Entra ID não podem ser alteradas pelo app: trate manualmente e ponha na whitelist.

## Solução de problemas

- **"Connect-ExchangeOnline ... não foi possível carregar o módulo"** ao rodar a fase 2: o Python usa `powershell.exe`
  (5.1). Se o módulo está no PowerShell 7, defina `POWERSHELL_EXE=pwsh` no `.env`.
- **E-mail "execução cancelada (extração do Exchange falhou)":** o `Connect-ExchangeOnline` falhou (certificado, módulo ou
  permissão). Rode `.\scripts\extrai_tipos_caixa.ps1` à mão para ver o erro completo.
- **`Authorization_RequestDenied` em uma conta:** quase sempre é conta com função administrativa.
- **Para reverter uma conta:** Entra ID → ativar "Account enabled"; reatribuir a licença; se foi convertida,
  `Set-Mailbox -Identity <email> -Type Regular`.
