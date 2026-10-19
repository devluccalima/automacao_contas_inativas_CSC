# Automação de Contas Inativas — Microsoft 365

Audita as caixas de e-mail do Microsoft 365, identifica contas inativas que ainda consomem licença e, de forma
escalonada, **bloqueia o login**, **converte a caixa em compartilhada** e **remove a licença**. A cada execução gera um
CSV de pendências e envia um e-mail com o resumo. Roda sozinha toda semana pelo Agendador de Tarefas do Windows.

## Como uma execução funciona

1. **Atualiza os tipos de caixa** no Exchange Online (`scripts/extrai_tipos_caixa.ps1`). Se falhar, a execução é
   **cancelada** e um e-mail de alerta é enviado: nunca se age com dados desatualizados.
2. Baixa o **relatório de atividade de e-mail** (Graph, janela D180) e os **dados do Entra ID** (data de criação e se o
   login está ativo).
3. **Classifica** cada conta (`core/classificacao_contas.py`) e gera o CSV de pendências.
4. **Processa** as candidatas, em simulação ou de verdade (`DRY_RUN`), respeitando `FASE`, `LOTE` e `LIMITE_MAX`.
5. **Envia o e-mail** com o resumo, a tabela do que foi (ou seria) feito e os CSVs em anexo. Sem candidatas, o e-mail
   diz "0 caixas inativas para efetuar a automação".

## Estágios e regras

A inatividade é medida pela **última atividade de e-mail** (enviar, ler, receber) e contada a partir da **data dos dados do
relatório** (`Report Refresh Date`), e não de hoje. Se essa data for mais velha que `MAX_ATRASO_RELATORIO_DIAS` (padrão 3),
a execução gera o relatório e o e-mail, mas **nenhuma conta é alterada**. Só contas com **licença paga** entram
(licenças gratuitas como Power Automate Free e Fabric Free não contam).

```
dias sem atividade:   0 ─────────── 35 ──────────── 55 ───────────▶
                       ativa         BLOQUEAR         CICLO COMPLETO
                                     (só login)       (bloquear → converter → remover licença)
```

| Situação | Resultado |
|---|---|
| E-mail na whitelist | `WHITELIST` (nunca é tocada) |
| Caixa de sistema (Room, Discovery, Scheduling), sem licença paga no relatório ou **sem nenhuma licença no Entra ID agora** (leitura ao vivo) | ocultada |
| Tipo de caixa não identificado | `REVISAR (TIPO DESCONHECIDO)` |
| `UserMailbox` inativa há mais de `DIAS_BLOQUEIO_USUARIO` (**35**) dias | `CANDIDATA A BLOQUEIO`, estágio `BLOQUEAR` |
| `UserMailbox` inativa há mais de `DIAS_CONVERSAO_USUARIO` (**55**) dias | `CANDIDATA A BLOQUEIO`, estágio `CICLO COMPLETO` |
| `SharedMailbox` inativa há mais de `DIAS_CONVERSAO_COMPARTILHADA` (**55**) dias | `CANDIDATA A BLOQUEIO`, estágio `CICLO COMPLETO` |
| **Nunca acessada** (sem nenhuma atividade) e com data de criação: a **idade da conta** vale como dias de inatividade, com os mesmos limites acima | `CANDIDATA A BLOQUEIO` (estágio `BLOQUEAR` ou `CICLO COMPLETO`); dentro do prazo, `CONTA RECENTE` |
| Nunca acessada e **sem data de criação** | `REVISAR (NUNCA ACESSADA)` |
| Candidata no estágio `BLOQUEAR` cujo login **já está bloqueado** | `BLOQUEADA (AGUARDANDO CONVERSÃO)` (não é pendência) |
| Demais casos | `ATIVA` |

Os limites são constantes no topo de `core/classificacao_contas.py`. Os limites comparam com `>` (com exatamente 35
dias a conta ainda é `ATIVA`).

**Janela de reclamação.** Com execução semanal, a conta é bloqueada na primeira execução em que passa de 35 dias e
convertida na primeira em que passa de 55: o intervalo real é de **2 a 3 semanas**. Isso só vale para quem cruza os
limites com a automação rodando. Contas que **já estavam acima de 55 dias** na primeira execução são bloqueadas,
convertidas e perdem a licença na mesma execução: para esse acúmulo inicial, a única proteção é o `LOTE`.

Contas `REVISAR` nunca são alteradas automaticamente: hoje são só as de **tipo de caixa desconhecido** e as nunca acessadas
sem data de criação. Elas reaparecem no CSV até alguém decidir (whitelist ou ação manual). Para voltar a deixar toda conta
nunca acessada em revisão manual, use `TRATAR_NUNCA_ACESSADAS = False` em `classificacao_contas.py`.

### Segurança em cada passo

Cada conta do ciclo completo só avança quando o passo anterior é **confirmado**:

| Passo | Confirmação | Se falhar |
|---|---|---|
| 0. **Trava de atividade recente** (Exchange, somente leitura) | `LastInteractionTime` ou `LastLogonTime` nos últimos 14 dias (`DIAS_ATIVIDADE_RECENTE`), **em contas com o login ativo** | `PULADA`: a conta não é alterada |
| 1. Bloquear o login (Graph) | Relê a conta até `accountEnabled=False` (até ~30 s) | `ERRO`; não converte nem mexe na licença |
| 2. Converter em compartilhada (Exchange) | Espera o tipo virar `SharedMailbox` (verifica a cada 5 s, até 90 s) | `ERRO`; a licença não é tocada |
| 3. Verificar se a licença pode sair | Caixa abaixo de 45 GB, sem arquivo morto e sem litigation hold/retenção | Converte, mas **mantém a licença** e diz o motivo |
| 4. Remover a licença (Graph) | Exige tipo `SharedMailbox` confirmado e relê até a licença sumir | `CONVERTIDA` com aviso "confira em Licenças" |

**Por que as duas travas existem.** O relatório de uso do Graph é um retrato em lote: o `Report Refresh Date` é o dia até o
qual ele vai, e a diferença para hoje já foi de 2 a 14 dias. Quem começa a usar o e-mail depois desse retrato aparece como
inativo ou "nunca acessado". Por isso: (1) dados velhos demais bloqueiam a execução inteira; (2) antes de qualquer ação, o
Exchange é consultado (somente leitura) e uma conta com interação ou logon recentes não é tocada, mesmo que o relatório a
mostre inativa. O `LastUserActionTime` não é usado porque atrasa; o `LastLogonTime` pode ser atualizado também por processos
em segundo plano, então, se gerar muitas contas puladas, remova-o de `CAMPOS_ATIVIDADE_EXCHANGE` em `conversao_e_licenca.py`.
Para agir mesmo assim, depois de conferir a conta, use `--forcar` nas ferramentas manuais.

**A trava só vale para contas com o login ativo.** Em conta com o login já bloqueado o dono não consegue entrar, então a
atividade vem de delegados ou processos (por exemplo, uma caixa compartilhada usada pela equipe). Isso não impede converter em
compartilhada nem tirar a licença, porque os delegados continuam com acesso; tamanho, arquivo morto e hold continuam sendo
verificados. Nesses casos o `Detalhe` registra "Atividade de delegados ou processos ignorada (login já bloqueado)". Se o login
não puder ser lido no Entra ID, a trava vale.

**O que foi comprovado no ambiente (leitura de 16/10/2026).** Receber e-mail não atualiza nenhum dos dois campos: uma caixa
que recebe milhares de mensagens sem uso manteve o último logon em 21/08 e nunca teve interação (o ano 1601 significa "nunca"). Em
compensação, caixas funcionais usadas por delegados ou integrações aparecem ativas no Exchange mesmo com zero ações no relatório
de uso, e a trava as protege. O campo `LastLoggedOnUserAccount` vem vazio neste tenant, então não dá para saber *quem* entrou.
Os horários do `Get-MailboxStatistics` vêm em **UTC** (Brasília = UTC−3, ou seja, 17:41 no Exchange é 14:41 em Brasília); o
script os trata como UTC e o `Detalhe` mostra o horário do servidor.

Outras travas: modo padrão é simulação; `LIMITE_MAX` aborta se houver candidatas demais; o processamento para após 5
erros seguidos ou na primeira falha do script do Exchange; contas sincronizadas com o AD local e contas com função
administrativa no Entra ID não são alteradas (`PULADA` / `ERRO`). Licenças herdadas de grupo não são removidas
(a caixa é convertida e o `Detalhe` avisa para tirar a conta do grupo de licenciamento).

## O que o e-mail e os CSVs mostram

- **E-mail:** o assunto traz as contagens da execução (licenças recuperadas ou que seriam, só bloqueio, protegidas por atividade
  recente, erros) e o resumo repete esses números; abaixo, resumo (candidatas por estágio, já bloqueadas aguardando conversão, ativas, recentes, revisão manual), a
  tabela do que foi processado e os anexos. Em simulação, o assunto começa com `[SIMULAÇÃO]`.
- **`relatorios/Relatorio_Inativos_*.csv`:** só pendências (candidatas e revisão manual), com data de criação, última
  atividade, login ativo e estágio. Sem pendências, o arquivo não é gerado. Para incluir todas as contas, use
  `CSV_SOMENTE_PENDENCIAS = False` em `classificacao_contas.py`.
- **`relatorios/Bloqueios_*.csv`:** uma linha por conta processada, com o resultado e o detalhe.

| Resultado | Significado |
|---|---|
| `SIMULADO` | Simulação: nada foi alterado |
| `BLOQUEADA` | Login bloqueado (e confirmado) nesta execução |
| `CONVERTIDA` | Caixa convertida em compartilhada, mas a licença **ficou** (veja o `Detalhe`) |
| `LICENCA_REMOVIDA` | Ciclo completo concluído |
| `JA_BLOQUEADA` | Já estava bloqueada (não aparece na tabela do e-mail) |
| `PULADA` | Nada foi feito (licença mantida, conta sincronizada com o AD etc.): veja o `Detalhe` |
| `ERRO` | Falhou em algum passo; o `Detalhe` traz o passo entre colchetes |

## Estrutura

```
automacao_contas_inativas/
├── main.py                          Orquestrador: roda tudo na ordem (de qualquer pasta)
├── requirements.txt                 Dependências: msal, requests, pandas, python-dotenv
├── .env                             Segredos e controles (NÃO versionar; modelo em .env.example)
├── config/
│   ├── tipos_caixa_exchange.csv     Gerado a cada execução por scripts/extrai_tipos_caixa.ps1
│   └── whitelist.csv                Coluna "email": contas que nunca são tocadas (NÃO versionar)
├── scripts/
│   ├── extrai_tipos_caixa.ps1       Exporta o tipo de cada caixa (UserMailbox, SharedMailbox...)
│   ├── converter_para_compartilhada.ps1   Converte a caixa e verifica se a licença pode sair
│   ├── executar_automacao.ps1       Usado pelo Agendador: roda o main.py e grava o log em logs/
│   ├── registrar_tarefa_agendada.ps1      Cria a tarefa semanal (padrão: quarta 19:00)
│   └── verificar_ambiente.ps1       Confere .env, .NET, Python, módulo, certificado, rede e tarefa
├── ferramentas/
│   ├── processar_conta_unica.py     Roda o ciclo completo em UMA conta específica
│   └── processar_lista_contas.py    Roda o ciclo completo em uma LISTA de contas já revisadas por você
├── core/
│   ├── graph_auth.py                Autenticação no Microsoft Graph
│   ├── extracao_tipos_caixa.py      Roda a extração do Exchange e valida o CSV
│   ├── graph_relatorio_email.py     Relatório D180, dados do Entra ID e envio do e-mail HTML
│   ├── classificacao_contas.py      Cruza os dados e classifica cada conta (limites de dias)
│   ├── bloqueio_login.py            Bloqueio do login (Graph)
│   └── conversao_e_licenca.py       Bloquear → converter em compartilhada → remover licença
├── relatorios/                      Saída: Relatorio_Inativos_*.csv e Bloqueios_*.csv (NÃO versionar)
└── logs/                            Saída do agendamento: execucao_AAAAMMDD_HHMM.log (90 dias)
```

## Configuração (`.env`)

Use o `.env.example` como modelo. O `.env` **prevalece** sobre variáveis do terminal.

| Variável | Efeito |
|---|---|
| `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET` | App registration no Entra ID (Graph) |
| `EMAIL_REMETENTE` | Caixa que envia o e-mail (uma só) |
| `EMAIL_DESTINATARIO` | Um ou vários endereços, separados por vírgula ou ponto e vírgula |
| `DRY_RUN` | `true` (padrão) só simula; só altera contas com `false` |
| `FASE` | `1` só bloqueia o login de **todas** as candidatas (freio geral); `2` escalonado por estágio |
| `LIMITE_MAX` | Se houver mais candidatas que isso, aborta o bloqueio (protege contra relatório anômalo) |
| `LOTE` | Quantas contas alterar por execução; `0` = sem limite. Contas `JA_BLOQUEADA`, `PULADA` e `ERRO` não gastam o lote |
| `MAX_ATRASO_RELATORIO_DIAS` | Padrão 3. Se os dados do relatório de uso forem mais velhos que isso, nenhuma conta é alterada |
| `ATUALIZAR_TIPOS_CAIXA` | `true` (padrão) roda a extração antes de cada execução; `false` usa o CSV existente |
| `EXO_APP_ID`, `EXO_THUMBPRINT`, `EXO_ORGANIZATION` | Autenticação por certificado no Exchange Online |
| `POWERSHELL_EXE` | Opcional: `pwsh` se o módulo estiver no PowerShell 7 (no servidor, deixe sem) |

Cuidados com o arquivo: comentários só em linhas próprias (nunca no fim de uma linha de valor), sem espaços em volta
do `=` e sem aspas. No servidor, edite com o PowerShell **elevado** (a trava de permissões só concede acesso a
Administrators, SYSTEM e à conta de serviço).

## Rodar manualmente

```powershell
python main.py                                                            # execução completa
python ferramentas\processar_conta_unica.py conta@dominio.com             # simulação em UMA conta (só lê)
python ferramentas\processar_conta_unica.py conta@dominio.com --real      # executa; pede para digitar o e-mail
python ferramentas\processar_lista_contas.py lista.txt                    # simulação em uma lista de contas (só lê)
python ferramentas\processar_lista_contas.py lista.txt --real             # executa; pede para digitar a quantidade
```

As ferramentas ignoram a classificação: agem nas contas informadas (a de lista pula as que estão na whitelist). Ambas passam pela trava de atividade recente do Exchange; `--forcar` a desliga (só use se você mesmo conferiu a conta). O arquivo da
lista aceita um e-mail por linha ou linhas coladas do relatório (vale o primeiro e-mail de cada linha) e processa em
**blocos** (`--bloco N`, padrão 8; cada conta leva de 20 a 30 s no Exchange). O resultado é salvo a cada bloco e as contas
que já ficaram sem licença são puladas, então, se você interromper (Ctrl+C) ou algo falhar, basta rodar o mesmo comando
de novo. No servidor, rode como a **conta de serviço** (o certificado do Exchange está no
perfil dela) e longe do horário da tarefa agendada, para as duas execuções não se sobreporem.

## Operação

### Rotina toda semana

- Confira o e-mail de quarta à noite. Se **não chegou**, a falha foi antes do Python (senha da conta de serviço,
  certificado, servidor desligado): veja `logs\` e o histórico da tarefa.
- Procure `ERRO` e `PULADA` no `Bloqueios_*.csv` e trate os detalhes (licença herdada de grupo, conta sincronizada com
  o AD, arquivo morto ou hold).
- Revise as contas `REVISAR (NUNCA ACESSADA)` e `REVISAR (TIPO DESCONHECIDO)`: ponha na whitelist ou trate à mão.
- Atenda reclamações (veja "Reverter uma conta") e ponha na whitelist quem precisar ficar fora.

### Implantação gradual (lotes)

A primeira execução com muitas contas acima de 55 dias deve ser feita em lotes, supervisionada. Plano usado:

| Quando | `LOTE` | Observação |
|---|---|---|
| 14/10 (tarde) | 10 | Primeiro lote real em produção, supervisionado: 11 caixas processadas |
| 14/10 (noite) | 10 | Disparo automático da tarefa |
| 15/10 | 20 | Rodada supervisionada |
| a partir de 21/10 | 0 | Operação semanal sem limite de lote |

Quando o acúmulo inicial acabar e o `LOTE` passar a `0`, a proteção contra uma execução anômala é o **`LIMITE_MAX`**.
Ajuste-o para algo próximo do número normal de candidatas por semana (as contas que ficam `PULADA` toda semana também
contam), com folga, para o bloqueio abortar se o relatório vier estranho.

### Reverter uma conta

1. **Entra ID:** ativar "Account enabled" e **reatribuir a licença** (o nome está na coluna `Licenças` do relatório do
   dia: guarde a pasta `relatorios\`).
2. **Exchange**, se a caixa foi convertida (faça depois de reatribuir a licença):
   `Set-Mailbox -Identity <email> -Type Regular`
3. Ponha a conta na **whitelist**, se ela for ficar sem usar o e-mail por um tempo. Como o relatório de atividade
   atrasa 1 a 2 dias, a whitelist evita um novo bloqueio se a reativação for perto da próxima execução.

## Agendamento (Agendador de Tarefas)

A tarefa `Automacao Contas Inativas M365` roda **toda quarta às 19:00** como a conta de serviço, executando
`scripts\executar_automacao.ps1`, que roda o `main.py` e grava `logs\execucao_*.log`.

```powershell
# Criar (ou recriar) a tarefa; pede a senha da conta de serviço
.\scripts\registrar_tarefa_agendada.ps1 -Usuario "SHOPPING\svc.autm365"
# Para a primeira execução automática cair só numa data futura
.\scripts\registrar_tarefa_agendada.ps1 -Usuario "SHOPPING\svc.autm365" -PrimeiraExecucao 2026-10-21

# Disparar à mão e acompanhar (PowerShell elevado, com a sua conta de administrador)
Start-ScheduledTask -TaskName "Automacao Contas Inativas M365"
Get-ScheduledTaskInfo -TaskName "Automacao Contas Inativas M365" | Select LastRunTime, LastTaskResult, NextRunTime
Get-Content (Get-ChildItem logs\*.log | Sort LastWriteTime | Select -Last 1) -Tail 30
```

`LastTaskResult` **0** = sucesso; `267009` = em execução; `267011` = ainda não rodou. Para pausar:
`Disable-ScheduledTask -TaskName "Automacao Contas Inativas M365"` (e `Enable-` depois: uma tarefa pausada não roda).

## Implantação no servidor (Windows Server 2016)

Depois de cada etapa, rode `.\scripts\verificar_ambiente.ps1` **como a conta de serviço**: ele mostra `[OK]`,
`[ATENCAO]` ou `[FALHA]` para cada item. Use sempre o **servidor membro** do domínio (nunca um controlador de domínio).

1. **Pré-requisitos** (PowerShell como administrador):
   - **.NET Framework 4.8** (o Server 2016 vem com 4.6.2; o módulo do Exchange exige 4.7.2 ou superior). Reinicie.
   - **Python para todos os usuários:** no instalador, "Customize installation" → "Install Python for all users"
     (fica em `C:\Program Files\Python313`). Instalado só no seu perfil, a conta de serviço não consegue executá-lo
     (`Access is denied` no `.venv`).
   - **Módulo do Exchange** no Windows PowerShell 5.1:
     ```powershell
     [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
     Install-PackageProvider -Name NuGet -MinimumVersion 2.8.5.201 -Force
     Install-Module ExchangeOnlineManagement -Scope AllUsers -Force
     powershell.exe -NoProfile -Command "Import-Module ExchangeOnlineManagement; (Get-Module ExchangeOnlineManagement).Version"
     ```
2. **Conta de serviço no AD** (`svc.autm365`): sem grupos privilegiados. No servidor:
   - direito **"Fazer logon como um trabalho em lote"** (`secpol.msc` → Diretivas Locais → Atribuição de Direitos de
     Usuário; se uma GPO controlar o direito, inclua a conta na GPO). Sem ele a tarefa falha com `0x80070569`;
   - acesso RDP **temporário** para importar o certificado e testar como ela:
     `net localgroup "Remote Desktop Users" SHOPPING\svc.autm365 /add` (revogue depois com `/delete`);
   - permissão de modificação na pasta do projeto e leitura no `.env` (passo 3).
   Use a **conta de serviço**, não a sua conta de administrador: o login terminado em `$` quebra caminhos de perfil
   no módulo do Exchange, e o Agendador deixaria de rodar quando a sua senha mudasse.
3. **Projeto** em `C:\Automacoes\automacao_contas_inativas`:
   ```powershell
   & "C:\Program Files\Python313\python.exe" -m venv .venv      # caminho completo, para não pegar um Python de perfil
   .\.venv\Scripts\python.exe -m pip install --upgrade pip
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   icacls C:\Automacoes\automacao_contas_inativas /grant "SHOPPING\svc.autm365:(OI)(CI)M"
   icacls .env /inheritance:r /grant "Administrators:(F)" /grant "SYSTEM:(F)" /grant "SHOPPING\svc.autm365:(R)"
   ```
   Copie à mão o que o git não leva: **`.env`** e **`config\whitelist.csv`**. Para editar o `.env` depois da trava, use
   um PowerShell **elevado** (`Start-Process notepad.exe -ArgumentList '<caminho>\.env' -Verb RunAs`).
4. **Certificado do Exchange:** exporte do PC onde ele já funciona e importe **no perfil da conta de serviço**
   (conectada por RDP como ela), depois apague o `.pfx` dos dois lugares. O thumbprint deve ser o do `.env`.
   ```powershell
   Export-PfxCertificate -Cert Cert:\CurrentUser\My\<THUMBPRINT> -FilePath exo.pfx -Password (Read-Host -AsSecureString)
   Import-PfxCertificate -FilePath .\exo.pfx -CertStoreLocation Cert:\CurrentUser\My -Password (Read-Host -AsSecureString)
   ```
   Se o certificado estiver em `Cert:\LocalMachine\My`, exporte de lá. Se a chave não for exportável, gere um novo
   certificado e carregue a parte pública no registro do app.
5. **Rede, TLS e horário:** saída HTTPS para `login.microsoftonline.com`, `graph.microsoft.com` e
   `outlook.office365.com`. Os scripts do Exchange habilitam o **TLS 1.2** por conta própria (o padrão do .NET no
   Server 2016 pode ser `Ssl3, Tls`, e a conexão cai com "connection was forcibly closed"). Confira o fuso com
   `tzutil /g` (Brasília = `E. South America Standard Time`).
6. **Teste manual como a conta de serviço**, com `DRY_RUN=true`: `.\scripts\verificar_ambiente.ps1`,
   `.\scripts\extrai_tipos_caixa.ps1` e `.\.venv\Scripts\python.exe main.py`.
7. **Tarefa agendada** (seção anterior). Valide primeiro em simulação, depois libere `DRY_RUN=false`.
8. Para **atualizar o código**: copie os arquivos alterados (ou `git pull`) e rode uma simulação antes de voltar ao modo real.
   Lembre que o servidor tem a **própria cópia** do código, do `.env` e da `whitelist.csv`.

## Permissões necessárias

- **Graph (aplicativo, com consentimento do administrador):** `Mail.Send`, `User.ReadWrite.All` (inclui a leitura de
  usuários, datas de criação e estado do login) e permissão de leitura de relatórios (`Reports.Read.All`).
- **Exchange Online:** `Exchange.ManageAsApp` e uma função que permita `Set-Mailbox` (por exemplo, Exchange
  Administrator) para o service principal do certificado.
- Contas com função administrativa no Entra ID não podem ser alteradas por um app com essas permissões: trate à mão e
  ponha na whitelist.

## Manutenção

| Item | O que fazer |
|---|---|
| Segredo do app (`AZURE_CLIENT_SECRET`) | Vence (em geral entre 6 e 24 meses). Se vencer, a execução e o e-mail de alerta falham juntos, sem aviso: veja a data em Entra → Registros de aplicativo → Certificados e segredos e crie um lembrete |
| Certificado do Exchange | Válido até 28/09/2036; o `verificar_ambiente.ps1` avisa com 60 dias |
| Senha da conta de serviço | Se mudar ou expirar, a tarefa para: recrie com `registrar_tarefa_agendada.ps1` |
| Windows Server 2016 | Fim do suporte estendido em **12/01/2027**: planeje a migração (clonar o projeto, copiar `.env` e `whitelist.csv`, importar o certificado e recriar a tarefa) |
| Acesso RDP da conta de serviço | Revogue quando a operação estabilizar |
| Pasta `relatorios\` | Guarde: a coluna `Licenças` é a referência para reatribuir uma licença removida |
| Whitelist | Revise de tempos em tempos; é cópia local em cada máquina |

## Solução de problemas

| Sintoma | Causa provável e correção |
|---|---|
| `LastTaskResult` 267011 e a tarefa nunca roda | Veja os eventos do Agendador (`Get-WinEvent -LogName Microsoft-Windows-TaskScheduler/Operational`) |
| Evento 104/101 com `2147943785` (`0x80070569`) | A conta de serviço não tem o direito "Fazer logon como um trabalho em lote" (ou a GPO o nega) |
| A execução parece parada, sem saída por minutos | O Exchange leva 20 a 30 s por conta. A ferramenta de lista mostra o progresso por bloco, e o log da tarefa mostra as linhas `[exchange]` de cada conta. Não é timeout: o limite é de 2 minutos por conta mais 5 |
| Interrompi (Ctrl+C) uma ferramenta ou a execução no meio | Sem risco: a ordem (bloquear → converter → remover a licença) é retomável. Rode de novo: a conta já convertida segue para a verificação e a remoção da licença, e as sem licença são puladas |
| `Access is denied` ao disparar a tarefa ou ao editar o `.env` | A janela não está **elevada**. Abra o PowerShell como administrador (confira `High Mandatory Level` em `whoami /groups`) |
| `Error Acquiring Token ... connection was forcibly closed` no Exchange | TLS antigo no .NET: use os scripts atuais (habilitam o TLS 1.2) |
| `Connect-ExchangeOnline ... não foi possível carregar o módulo` | .NET abaixo de 4.7.2, ou módulo instalado só no PowerShell 7 (`POWERSHELL_EXE=pwsh`) |
| `did not find executable ... Python313\python.exe: Access is denied` | Python instalado só para o seu usuário: reinstale para todos e recrie o `.venv` |
| E-mail "execução cancelada (extração do Exchange falhou)" | Rode `.\scripts\extrai_tipos_caixa.ps1` à mão como a conta de serviço para ver o erro (certificado, módulo, permissão) |
| `N candidatas excedem o limite de segurança` | Revise o relatório; suba `LIMITE_MAX` só se o número for esperado |
| E-mail não chega com vários destinatários | Confirme que `core\graph_relatorio_email.py` é a versão com `_destinatarios` |
| Acentos trocados no log | Use o `executar_automacao.ps1` atual (força UTF-8) |
| `Authorization_RequestDenied` em uma conta | Quase sempre é conta com função administrativa |
| Conta `PULADA` com "Atividade recente: última interação em ...; último logon em ... (Exchange)" | A trava funcionou: o Exchange viu uso nos últimos 14 dias. "Somente logon, sem interação do usuário" indica sincronização ou processo, não pessoa: confira antes de forçar. Se a conta é mesmo ociosa, confira e use `--forcar` na ferramenta manual |
| E-mail "Os dados do relatório de uso estão velhos" | O relatório do Graph está atrasado além de `MAX_ATRASO_RELATORIO_DIAS`. Nada foi alterado: aguarde ele atualizar (a data aparece no resumo do e-mail) |
| Conta aparece no relatório de uso com licença que já foi removida | O relatório de uso atrasa e mostra a licença antiga. A automação lê as licenças **ao vivo** no Entra ID e oculta quem já está sem nenhuma; se ainda aparecer, confira se o `get_dados_entra` obteve os dados (mensagem "Dados do Entra ID obtidos" no log) |
| Conta `PULADA` toda semana | Conta sincronizada com o AD (desative no AD), arquivo morto/hold/tamanho (licença mantida de propósito) ou licença herdada de grupo |

## Limitações conhecidas

- A inatividade é medida **só pelo e-mail**. Quem usa só Teams, OneDrive ou SharePoint pode parecer inativo: confira os
  relatórios de uso desses serviços antes de liberar lotes grandes e use a whitelist.
- Caixas que só **recebem** e-mail (apelidos, caixas funcionais) podem aparecer como inativas. Como as contas nunca acessadas
  são tratadas automaticamente, uma caixa funcional criada e nunca usada por login será bloqueada e, passado o prazo,
  convertida: ponha na whitelist as que devem ficar como estão. Contas criadas antes da data de entrada de um funcionário
  também podem ser bloqueadas se ficarem sem uso além de 35 dias.
- Remover a licença afeta também Teams, OneDrive e SharePoint do usuário: confirme a política de retenção do OneDrive.
- Licenças herdadas de grupo e contas sincronizadas com o AD local exigem ação manual.
- As travas **reduzem** o risco, mas não o eliminam: elas olham a atividade da **caixa de e-mail**. Quem usa só Teams,
  OneDrive ou SharePoint, ou alguém que começa a usar depois da leitura, não é visto.
- A trava do Exchange vale para a fase 1, a fase 2 e as ferramentas manuais. Na fase 1 ela roda antes de qualquer bloqueio,
  lendo o Exchange de todas as candidatas (alguns minutos a mais).
- Conta criada antes da entrada do funcionário parece "nunca acessada": se ficar parada por mais de 35 dias, será bloqueada,
  a menos que haja logon ou interação recentes. Ponha na whitelist as contas provisionadas com antecedência.

## Histórico

| Data | Marco |
|---|---|
| 06/10/2026 | Bloqueio de login (fase 1) em produção, em lotes pequenos |
| 08/10/2026 | Ciclo completo (bloquear → converter → remover licença) validado em conta real |
| 13/10/2026 | Servidor Windows Server 2016 configurado: conta de serviço, certificado, TLS 1.2, tarefa agendada |
| 14/10/2026 | Tarefa validada em simulação; primeiro lote real em produção (11 caixas); rollout em lotes crescentes até a operação semanal |
| 15/10/2026 | Uma conta que acabara de começar a usar o e-mail foi processada por causa do atraso do relatório: criadas a trava de dados velhos (`Report Refresh Date`) e a trava de atividade recente no Exchange |