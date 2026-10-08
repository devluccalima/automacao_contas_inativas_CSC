<#
.SYNOPSIS
  Executa a automação completa (usado pelo Agendador de Tarefas) e grava a saída em logs\.
#>

$Raiz = Split-Path $PSScriptRoot -Parent
Set-Location $Raiz

$PastaLogs = Join-Path $Raiz 'logs'
New-Item -ItemType Directory -Force -Path $PastaLogs | Out-Null
$Log = Join-Path $PastaLogs ("execucao_{0:yyyyMMdd_HHmm}.log" -f (Get-Date))

# Python do ambiente virtual, se existir; senão o python do PATH
$Python = Join-Path $Raiz '.venv\Scripts\python.exe'
if (-not (Test-Path $Python)) { $Python = 'python' }

# Garante UTF-8 na saída do Python (os emojis do log quebram com a codificação padrão ao redirecionar)
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

& $Python 'main.py' 2>&1 | Out-File -FilePath $Log -Encoding utf8
$codigo = $LASTEXITCODE

# Mantém só os últimos 90 dias de logs
Get-ChildItem $PastaLogs -Filter 'execucao_*.log' |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-90) } |
    Remove-Item -Force

exit $codigo
