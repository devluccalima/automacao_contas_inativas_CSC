<#
.SYNOPSIS
  Fase 2 (parte Exchange): converte caixas de usuário em compartilhadas e verifica se a licença
  pode ser removida com segurança. Chamado pelo Python (core/conversao_e_licenca.py), uma vez por lote.

.PARAMETER EmailsJson        Arquivo JSON com a lista de e-mails (UserMailbox ou SharedMailbox).
.PARAMETER SaidaJson         Arquivo JSON onde o resultado de cada conta é gravado.
.PARAMETER SomenteVerificar  Não altera nada: só roda as verificações (usado no dry-run).
#>
param(
    [Parameter(Mandatory)][string]$EmailsJson,
    [Parameter(Mandatory)][string]$SaidaJson,
    [Parameter(Mandatory)][string]$AppId,
    [Parameter(Mandatory)][string]$Thumbprint,
    [Parameter(Mandatory)][string]$Organization,
    [switch]$SomenteVerificar,
    [int]$LimiteGB = 45,            # margem abaixo dos 50 GB de uma compartilhada sem licença
    [int]$TimeoutSegundos = 90      # tempo máximo esperando a conversão propagar
)

$ErrorActionPreference = 'Stop'
$resultados = @()

function Get-TamanhoBytes([string]$email) {
    $texto = (Get-MailboxStatistics -Identity $email).TotalItemSize.ToString()
    if ($texto -match '\(([^)]*)\sbytes\)') { return [int64]($Matches[1] -replace '[^\d]', '') }
    return $null
}

# Motivos para NÃO remover a licença (lista vazia = pode remover)
function Get-MotivosManterLicenca([string]$email) {
    $motivos = @()
    $mbx = Get-Mailbox -Identity $email

    $bytes = Get-TamanhoBytes $email
    if ($null -eq $bytes) { $motivos += 'não foi possível ler o tamanho da caixa' }
    elseif ($bytes -gt ([int64]$LimiteGB * 1GB)) { $motivos += "caixa acima de $LimiteGB GB" }

    $guidVazio = '00000000-0000-0000-0000-000000000000'
    if ("$($mbx.ArchiveStatus)" -eq 'Active' -or ("$($mbx.ArchiveGuid)" -ne '' -and "$($mbx.ArchiveGuid)" -ne $guidVazio)) {
        $motivos += 'arquivo morto ativo'
    }
    if ($mbx.LitigationHoldEnabled) { $motivos += 'litigation hold' }
    if ($mbx.InPlaceHolds -and @($mbx.InPlaceHolds).Count -gt 0) { $motivos += 'retenção/hold aplicado' }

    return $motivos
}

# Importa o módulo de forma explícita: se falhar, a mensagem mostra a causa real
Import-Module ExchangeOnlineManagement -ErrorAction Stop

Connect-ExchangeOnline -AppId $AppId -CertificateThumbprint $Thumbprint -Organization $Organization -ShowBanner:$false

try {
    $emails = Get-Content -Path $EmailsJson -Raw -Encoding UTF8 | ConvertFrom-Json

    foreach ($email in @($emails)) {
        $r = [ordered]@{
            Email              = "$email"
            TipoInicial        = $null
            TipoFinal          = $null
            Convertida         = $false
            PodeRemoverLicenca = $false
            Motivos            = ''
            Erro               = ''
        }
        try {
            $mbx = Get-Mailbox -Identity $email
            $r.TipoInicial = $mbx.RecipientTypeDetails.ToString()
            $r.TipoFinal   = $r.TipoInicial

            if ($r.TipoInicial -notin @('UserMailbox', 'SharedMailbox')) {
                throw "tipo de caixa não suportado: $($r.TipoInicial)"
            }

            if ($r.TipoInicial -eq 'UserMailbox' -and -not $SomenteVerificar) {
                Set-Mailbox -Identity $email -Type Shared

                # A conversão leva alguns segundos para propagar: espera confirmar antes de seguir
                $limite = (Get-Date).AddSeconds($TimeoutSegundos)
                do {
                    Start-Sleep -Seconds 5
                    $tipo = (Get-Mailbox -Identity $email).RecipientTypeDetails.ToString()
                } while ($tipo -ne 'SharedMailbox' -and (Get-Date) -lt $limite)

                $r.TipoFinal  = $tipo
                $r.Convertida = ($tipo -eq 'SharedMailbox')
                if (-not $r.Convertida) { throw "conversão não confirmada após $TimeoutSegundos s" }
            }

            $motivos = @(Get-MotivosManterLicenca $email)
            $r.Motivos = ($motivos -join '; ')
            $r.PodeRemoverLicenca = ($motivos.Count -eq 0)
        }
        catch {
            $r.Erro = $_.Exception.Message
        }
        $resultados += [pscustomobject]$r
    }
}
finally {
    Disconnect-ExchangeOnline -Confirm:$false
    ConvertTo-Json -InputObject @($resultados) -Depth 3 | Out-File -FilePath $SaidaJson -Encoding utf8
}
