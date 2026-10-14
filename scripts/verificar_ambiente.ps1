<#
.SYNOPSIS
  Verifica se o servidor está pronto para rodar a automação.
  Execute como a CONTA DE SERVIÇO (o certificado fica no perfil dela) e, de preferência, como administrador
  (para enxergar a tarefa agendada). Rode depois de cada etapa da implantação.
#>

$Raiz   = Split-Path $PSScriptRoot -Parent
$falhas = 0

function Resultado($status, $mensagem) {
    $cor = 'Red'
    if ($status -eq 'OK') { $cor = 'Green' }
    if ($status -eq 'ATENCAO') { $cor = 'Yellow' }
    Write-Host ("[{0}] {1}" -f $status, $mensagem) -ForegroundColor $cor
    if ($status -eq 'FALHA') { $script:falhas++ }
}

Write-Host "Verificando o ambiente em $Raiz como $env:USERDOMAIN\$env:USERNAME" -ForegroundColor Cyan
Write-Host ""

# TLS: o padrão do .NET Framework pode não incluir o TLS 1.2. Os scripts do projeto o habilitam por conta própria.
$tlsPadrao = [Net.ServicePointManager]::SecurityProtocol
if ($tlsPadrao -notmatch 'Tls12|SystemDefault') {
    Resultado 'ATENCAO' "O padrão do .NET nesta máquina é '$tlsPadrao' (sem TLS 1.2). Os scripts do projeto habilitam o TLS 1.2 por conta própria"
}
[Net.ServicePointManager]::SecurityProtocol = $tlsPadrao -bor [Net.SecurityProtocolType]::Tls12

# --- .env ---
$cfg = @{}
$envPath = Join-Path $Raiz '.env'
if (Test-Path $envPath) {
    Resultado 'OK' ".env encontrado"
    Get-Content -Path $envPath -Encoding UTF8 | ForEach-Object {
        $l = $_.Trim()
        if ($l -and -not $l.StartsWith('#') -and $l.Contains('=')) {
            $k, $v = $l -split '=', 2
            $cfg[$k.Trim()] = $v.Trim().Trim('"').Trim("'")
        }
    }
} else {
    Resultado 'FALHA' ".env não encontrado em $envPath"
}

foreach ($nome in 'AZURE_TENANT_ID', 'AZURE_CLIENT_ID', 'AZURE_CLIENT_SECRET', 'EMAIL_REMETENTE',
                  'EMAIL_DESTINATARIO', 'EXO_APP_ID', 'EXO_THUMBPRINT', 'EXO_ORGANIZATION') {
    if ($cfg[$nome]) { Resultado 'OK' "$nome preenchido" } else { Resultado 'FALHA' "$nome vazio ou ausente no .env" }
}
if ($cfg['POWERSHELL_EXE']) {
    Resultado 'ATENCAO' "POWERSHELL_EXE=$($cfg['POWERSHELL_EXE']): confirme que esse executável existe neste servidor (o padrão é powershell.exe)"
}
Resultado 'OK' ("Modo configurado: DRY_RUN={0} | FASE={1} | LOTE={2} | LIMITE_MAX={3}" -f $cfg['DRY_RUN'], $cfg['FASE'], $cfg['LOTE'], $cfg['LIMITE_MAX'])

# --- whitelist ---
if (Test-Path (Join-Path $Raiz 'config\whitelist.csv')) { Resultado 'OK' "config\whitelist.csv encontrado" }
else { Resultado 'FALHA' "config\whitelist.csv ausente (o git não leva esse arquivo: copie-o à mão)" }

# --- .NET Framework (o módulo do Exchange exige 4.7.2 ou superior no PowerShell 5.1) ---
$release = (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full' -ErrorAction SilentlyContinue).Release
if ($release -ge 528040) { Resultado 'OK' ".NET Framework 4.8 ou superior (release $release)" }
elseif ($release -ge 461808) { Resultado 'OK' ".NET Framework 4.7.2 ou superior (release $release)" }
else { Resultado 'FALHA' ".NET Framework abaixo de 4.7.2 (release $release): instale o 4.8 e reinicie" }

# --- Python / ambiente virtual / dependências ---
$py = Join-Path $Raiz '.venv\Scripts\python.exe'
if (Test-Path $py) {
    $ver = & $py --version 2>&1
    Resultado 'OK' "Ambiente virtual encontrado ($ver)"
    & $py -c "import msal, requests, pandas, dotenv" 2>$null
    if ($LASTEXITCODE -eq 0) { Resultado 'OK' "Dependências Python instaladas" }
    else { Resultado 'FALHA' "Faltam dependências: .\.venv\Scripts\python.exe -m pip install -r requirements.txt" }
} else {
    Resultado 'FALHA' "Ambiente virtual não encontrado ($py): python -m venv .venv"
}

# --- Módulo do Exchange Online ---
$mod = Get-Module -ListAvailable -Name ExchangeOnlineManagement | Sort-Object Version -Descending | Select-Object -First 1
if ($mod) { Resultado 'OK' "Módulo ExchangeOnlineManagement $($mod.Version) (PowerShell $($PSVersionTable.PSVersion))" }
else { Resultado 'FALHA' "Módulo ExchangeOnlineManagement não encontrado neste PowerShell" }

# --- Certificado do Exchange no perfil do usuário atual ---
$thumb = ("" + $cfg['EXO_THUMBPRINT']) -replace '\s', ''
if ($thumb) {
    $cert = Get-ChildItem Cert:\CurrentUser\My | Where-Object { $_.Thumbprint -eq $thumb.ToUpper() } | Select-Object -First 1
    if (-not $cert) {
        Resultado 'FALHA' "Certificado $thumb não está em Cert:\CurrentUser\My de $env:USERDOMAIN\$env:USERNAME (importe-o logado como a conta de serviço)"
    } elseif (-not $cert.HasPrivateKey) {
        Resultado 'FALHA' "Certificado encontrado, mas sem chave privada (reimporte o .pfx)"
    } else {
        $dias = [int]($cert.NotAfter - (Get-Date)).TotalDays
        $validade = $cert.NotAfter.ToString('dd/MM/yyyy')
        if ($dias -lt 0) { Resultado 'FALHA' "Certificado EXPIRADO em $validade" }
        elseif ($dias -lt 60) { Resultado 'ATENCAO' "Certificado expira em $dias dias ($validade): renove antes disso" }
        else { Resultado 'OK' "Certificado com chave privada, válido até $validade ($dias dias)" }
    }
}

# --- Fuso horário ---
$fuso = (Get-TimeZone).Id
if ($fuso -eq 'E. South America Standard Time') { Resultado 'OK' "Fuso horário de Brasília" }
else { Resultado 'ATENCAO' "Fuso horário é '$fuso': o horário da tarefa seguirá esse fuso" }

# --- Rede: não basta a porta abrir, o handshake TLS/HTTPS precisa funcionar ---
function Testar-Https($nomeHost) {
    try {
        Invoke-WebRequest -Uri "https://$nomeHost" -UseBasicParsing -TimeoutSec 20 -ErrorAction Stop | Out-Null
        return $true
    } catch {
        if ($_.Exception.Response) { return $true }   # o servidor respondeu (mesmo com erro HTTP): o TLS funcionou
        return $false
    }
}
foreach ($h in 'login.microsoftonline.com', 'graph.microsoft.com', 'outlook.office365.com') {
    if (Testar-Https $h) { Resultado 'OK' "HTTPS (TLS 1.2) funcionando para $h" }
    else { Resultado 'FALHA' "Falha de conexão HTTPS/TLS com $h (firewall, inspeção SSL ou proxy do usuário)" }
}

# --- Tarefa agendada ---
$nomeTarefa = 'Automacao Contas Inativas M365'
$tarefa = Get-ScheduledTask -TaskName $nomeTarefa -ErrorAction SilentlyContinue
if ($tarefa) {
    $info = Get-ScheduledTaskInfo -TaskName $nomeTarefa
    Resultado 'OK' "Tarefa agendada encontrada (estado: $($tarefa.State) | último resultado: $($info.LastTaskResult) | próxima execução: $($info.NextRunTime))"
} else {
    Resultado 'ATENCAO' "Tarefa '$nomeTarefa' não encontrada (ou sem permissão para vê-la). Se ainda não criou: scripts\registrar_tarefa_agendada.ps1"
}

Write-Host ""
if ($falhas -eq 0) { Write-Host "Ambiente pronto: nenhuma falha encontrada." -ForegroundColor Green }
else { Write-Host "$falhas falha(s) encontrada(s). Corrija e rode de novo." -ForegroundColor Red }
exit $(if ($falhas -eq 0) { 0 } else { 1 })