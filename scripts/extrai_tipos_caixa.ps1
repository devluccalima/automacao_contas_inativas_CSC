# ==============================================================================
# ETAPA 1: Extração de Tipos de Caixa (Autenticação via Certificado)
# Lê EXO_APP_ID, EXO_THUMBPRINT e EXO_ORGANIZATION do arquivo .env
# ==============================================================================

$ErrorActionPreference = 'Stop'   # qualquer falha encerra o script com código de saída diferente de 0

# --- Carrega o .env (procura na pasta do script e na pasta acima) ---
$CaminhoEnv = @("$PSScriptRoot\.env", "$PSScriptRoot\..\.env") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $CaminhoEnv) { throw "Arquivo .env não encontrado em $PSScriptRoot nem na pasta acima." }
$CaminhoEnv = (Resolve-Path $CaminhoEnv).Path
$Raiz = Split-Path $CaminhoEnv -Parent   # raiz do projeto = pasta onde está o .env

Get-Content -Path $CaminhoEnv -Encoding UTF8 | ForEach-Object {
    $linha = $_.Trim()
    if ($linha -and -not $linha.StartsWith('#') -and $linha.Contains('=')) {
        $nome, $valor = $linha -split '=', 2
        $nome  = $nome.Trim()
        $valor = $valor.Trim().Trim('"').Trim("'")
        # Não sobrescreve variável que já exista no ambiente (mesmo comportamento do python-dotenv)
        if (-not [Environment]::GetEnvironmentVariable($nome, 'Process')) {
            [Environment]::SetEnvironmentVariable($nome, $valor, 'Process')
        }
    }
}

$AppId        = $env:EXO_APP_ID
$Thumbprint   = $env:EXO_THUMBPRINT
$Organization = $env:EXO_ORGANIZATION

if (-not ($AppId -and $Thumbprint -and $Organization)) {
    throw "Defina EXO_APP_ID, EXO_THUMBPRINT e EXO_ORGANIZATION no arquivo .env"
}

# Caminho do CSV relativo à raiz do projeto (funciona de qualquer pasta, inclusive no Agendador de Tarefas)
$CaminhoCSV = Join-Path $Raiz "config\tipos_caixa_exchange.csv"
New-Item -ItemType Directory -Force -Path (Split-Path $CaminhoCSV -Parent) | Out-Null

# Conexão silenciosa via Certificado
Connect-ExchangeOnline -CertificateThumbprint $Thumbprint -AppID $AppId -Organization $Organization -ShowBanner:$false

Write-Host "Conectado ao Exchange silenciosamente. Extraindo caixas..." -ForegroundColor Cyan

# Extrai os dados (Super rápido)
Get-Mailbox -ResultSize Unlimited | Select-Object PrimarySmtpAddress, RecipientTypeDetails | Export-Csv -Path $CaminhoCSV -NoTypeInformation -Encoding UTF8 -Delimiter ";"

Disconnect-ExchangeOnline -Confirm:$false
Write-Host "Extração concluída com sucesso em: $CaminhoCSV" -ForegroundColor Green
