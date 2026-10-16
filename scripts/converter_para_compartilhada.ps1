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
# O Windows PowerShell 5.1 / .NET Framework pode usar TLS antigo por padrão; o Entra ID e o Exchange Online exigem TLS 1.2
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
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

# Converte a data do Exchange para UTC em texto; o ano 1601 é o valor do Exchange para "nunca" e vira vazio
function Get-DataUtc($valor) {
    if (-not $valor) { return '' }
    try { $d = [datetime]$valor } catch { return '' }
    if ($d.Year -le 1900) { return '' }
    # O Exchange devolve horários em UTC. Sem tipo definido, trata como UTC: ToUniversalTime() assumiria hora local
    # e somaria o fuso (3 horas no horário de Brasília).
    if ($d.Kind -eq [System.DateTimeKind]::Local) { $d = $d.ToUniversalTime() }
    else { $d = [datetime]::SpecifyKind($d, [System.DateTimeKind]::Utc) }
    return $d.ToString('yyyy-MM-ddTHH:mm:ssZ')
}

Connect-ExchangeOnline -AppId $AppId -CertificateThumbprint $Thumbprint -Organization $Organization -ShowBanner:$false

try {
    $emails = Get-Content -Path $EmailsJson -Raw -Encoding UTF8 | ConvertFrom-Json

    $total = @($emails).Count
    $n = 0

    foreach ($email in @($emails)) {
        $n++
        Write-Host ("[{0}/{1}] {2}" -f $n, $total, $email)
        $r = [ordered]@{
            Email              = "$email"
            TipoInicial        = $null
            TipoFinal          = $null
            Convertida         = $false
            PodeRemoverLicenca = $false
            Motivos            = ''
            Erro               = ''
            UltimoLogon        = ''
            UltimaInteracao    = ''
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

            # Atividade em tempo quase real (o relatório de uso do Graph chega com ~2 dias de atraso)
            $stats = Get-MailboxStatistics -Identity $email -ErrorAction SilentlyContinue
            if ($stats) {
                $r.UltimoLogon     = Get-DataUtc $stats.LastLogonTime
                $r.UltimaInteracao = Get-DataUtc $stats.LastInteractionTime
            }

            $motivos = @(Get-MotivosManterLicenca $email)
            $r.Motivos = ($motivos -join '; ')
            $r.PodeRemoverLicenca = ($motivos.Count -eq 0)
        }
        catch {
            $r.Erro = $_.Exception.Message
        }
        $resultados += [pscustomobject]$r
        $ultimo = @($r.UltimoLogon, $r.UltimaInteracao) | Where-Object { $_ } | Sort-Object -Descending | Select-Object -First 1
        if (-not $ultimo) { $ultimo = "nenhuma" }
        $situacao = "tipo=$($r.TipoFinal) | última atividade no Exchange=$ultimo | licença pode sair=$($r.PodeRemoverLicenca)"
        if ($r.Erro) { $situacao = "ERRO: $($r.Erro)" }
        Write-Host "      $situacao"
    }
}
finally {
    Disconnect-ExchangeOnline -Confirm:$false
    ConvertTo-Json -InputObject @($resultados) -Depth 3 | Out-File -FilePath $SaidaJson -Encoding utf8
}