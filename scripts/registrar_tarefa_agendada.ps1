<#
.SYNOPSIS
  Cria (ou recria) a tarefa semanal da automação no Agendador de Tarefas do Windows.

.EXAMPLE
  .\scripts\registrar_tarefa_agendada.ps1 -Usuario "DOMINIO\svc-automacao"
  .\scripts\registrar_tarefa_agendada.ps1 -Usuario "DOMINIO\svc-automacao" -Dia Wednesday -Hora 19:00

  Pede a senha da conta de serviço (ela não fica gravada em script nem em histórico).
  A conta precisa ter o direito "Fazer logon como um trabalho em lote" neste servidor.
#>
param(
    [Parameter(Mandatory)][string]$Usuario,
    [ValidateSet('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday')]
    [string]$Dia = 'Wednesday',
    [string]$Hora = '19:00',
    [string]$NomeTarefa = 'Automacao Contas Inativas M365'
)

$Raiz   = Split-Path $PSScriptRoot -Parent
$Script = Join-Path $PSScriptRoot 'executar_automacao.ps1'
if (-not (Test-Path $Script)) { throw "Script não encontrado: $Script" }

$cred  = Get-Credential -UserName $Usuario -Message 'Senha da conta que vai executar a tarefa'
$senha = $cred.GetNetworkCredential().Password

$acao = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$Script`"" `
    -WorkingDirectory $Raiz

$gatilho = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $Dia -At $Hora

# Recupera execução perdida (servidor desligado no horário), não sobrepõe execuções e limita a 3 h
$config = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 3)

Register-ScheduledTask -TaskName $NomeTarefa -Action $acao -Trigger $gatilho -Settings $config `
    -User $cred.UserName -Password $senha -RunLevel Limited -Force | Out-Null

Write-Host "Tarefa '$NomeTarefa' criada: toda $Dia às $Hora, executando como $($cred.UserName)." -ForegroundColor Green
Write-Host "Para testar agora:  Start-ScheduledTask -TaskName '$NomeTarefa'"
Write-Host "Para ver o resultado: Get-ScheduledTaskInfo -TaskName '$NomeTarefa' | Select LastRunTime, LastTaskResult"