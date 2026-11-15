<#
.SYNOPSIS
  Walk2AI: prepara e faz o primeiro pedido real de IA (Windows PowerShell 5.1 ou 7).

.DESCRIPTION
  1. Inicia sessão como administrador.
  2. Confirma que há uma taxa USD->EUR em vigor (o backend segue a taxa de referência
     diária do BCE); se não houver, pede-lhe que a vá buscar (ou usa 0.905 se o BCE
     não responder).
  3. Confirma junto de cada fornecedor configurado que a sua chave tem acesso ao modelo
     mais barato (gratuito) e ativa-o.
  4. Se o saldo for baixo, credita 500 WC na sua carteira (ajuste de administrador).
  5. Só depois de confirmar: envia UMA mensagem curta a cada modelo ativado.
     É um pedido real, pago pelo saldo pré-pago do fornecedor (uma fração de cêntimo).

  Uso, na pasta do projeto:
    powershell -ExecutionPolicy Bypass -File infrastructure\scripts\first-ai-request.ps1
#>
param(
    [string]$BaseUrl = "http://localhost:8000/api/v1"
)

$ErrorActionPreference = "Stop"
$Session = New-Object Microsoft.PowerShell.Commands.WebRequestSession
$Models = @("claude-haiku-5-5", "gpt-6-luna")   # o mais barato de cada fornecedor

function Invoke-Api {
    param([string]$Method, [string]$Path, $Body = $null)
    $request = @{
        Method      = $Method
        Uri         = "$BaseUrl$Path"
        WebSession  = $Session
        ContentType = "application/json; charset=utf-8"
    }
    if ($null -ne $Body) {
        $json = $Body | ConvertTo-Json -Depth 10 -Compress
        $request.Body = [Text.Encoding]::UTF8.GetBytes($json)
    }
    try {
        return Invoke-RestMethod @request
    } catch {
        $detail = $_.ErrorDetails.Message
        if (-not $detail) { $detail = $_.Exception.Message }
        throw "$Method $Path falhou: $detail"
    }
}

# Windows PowerShell 5.1 returns a JSON array as ONE object; this lists its items one by
# one in every PowerShell version.
function Get-List {
    param([string]$Path)
    return @(Invoke-Api GET $Path | ForEach-Object { $_ })
}

function Write-Step([string]$Text) { Write-Host ""; Write-Host "== $Text" -ForegroundColor Cyan }

# ------------------------------------------------------------------ 1. sessão
Write-Step "Iniciar sessão no Walk2AI ($BaseUrl)"
try {
    Invoke-RestMethod -Uri "$BaseUrl/health/ready" -WebSession $Session | Out-Null
} catch {
    throw "O backend não responde em $BaseUrl. Está a correr? (docker compose up -d --build)"
}
$email = Read-Host "Email da sua conta Walk2AI"
$secure = Read-Host "Palavra-passe" -AsSecureString
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try { $password = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
Invoke-Api POST "/auth/login" @{ email = $email; password = $password } | Out-Null
$password = $null
$me = Invoke-Api GET "/auth/me"
if ($me.role -ne "admin") { throw "Esta conta não é de administrador." }
Write-Host "Sessão iniciada como $($me.email)."

# ------------------------------------------------------------------ 2. taxa USD->EUR
# O backend segue a taxa de referência diária do BCE sozinho. Aqui só se confirma que
# existe uma em vigor; se não existir, pede-se ao backend que a vá buscar agora.
Write-Step "Taxa de câmbio USD -> EUR"
function Get-UsdRate {
    return @(Get-List "/admin/exchange-rates" |
        Where-Object { $_.base_currency -eq "USD" -and $_.quote_currency -eq "EUR" -and $null -eq $_.effective_until })
}
$current = @(Get-UsdRate)
if ($current.Count -eq 0) {
    $refresh = Invoke-Api POST "/admin/exchange-rates/refresh"
    Write-Host $refresh.detail
    $current = @(Get-UsdRate)
}
if ($current.Count -eq 0) {
    Invoke-Api POST "/admin/exchange-rates" @{
        base_currency = "USD"; quote_currency = "EUR"; rate = "0.905"; source = "manual (BCE indisponível)"
    } | Out-Null
    Write-Host "BCE indisponível: definida à mão 1 USD = 0.905 EUR." -ForegroundColor Yellow
} else {
    Write-Host "Em vigor: 1 USD = $($current[0].rate) EUR ($($current[0].source))."
}

# ------------------------------------------------------------------ 3. modelos
Write-Step "Confirmar e ativar modelos"
$catalogue = Get-List "/admin/models"
$ready = @()
foreach ($identifier in $Models) {
    $model = $catalogue | Where-Object { $_.model_identifier -eq $identifier } | Select-Object -First 1
    if (-not $model) { Write-Host "$identifier não está no catálogo." -ForegroundColor Yellow; continue }
    try {
        $check = Invoke-Api POST "/admin/models/$($model.id)/check"
    } catch {
        Write-Host "$identifier ($($model.provider)): ignorado. $($_.Exception.Message)" -ForegroundColor Yellow
        continue
    }
    if (-not $check.available) {
        Write-Host "$identifier : a sua chave não tem acesso ($($check.detail))." -ForegroundColor Yellow
        continue
    }
    $updated = Invoke-Api PUT "/admin/models/$($model.id)" @{ enabled = $true }
    if ($updated.usable) {
        Write-Host "$identifier : disponível e ativado." -ForegroundColor Green
        $ready += $updated
    } else {
        Write-Host "$identifier : ativado mas ainda indisponível: $($updated.unavailable_reasons -join ', ')" -ForegroundColor Yellow
    }
}
if ($ready.Count -eq 0) { throw "Nenhum modelo ficou disponível. Veja as mensagens acima." }

# ------------------------------------------------------------------ 4. créditos
Write-Step "Créditos"
$wallet = Invoke-Api GET "/wallet"
if ($wallet.available_wc -lt 100) {
    Invoke-Api POST "/admin/wallet/adjustments" @{
        user_id = $me.id; amount_wc = 500; reason = "Primeiro teste da IA"
        idempotency_key = "first-ai-test-$(Get-Date -Format 'yyyyMMddHHmmss')"
    } | Out-Null
    $wallet = Invoke-Api GET "/wallet"
}
Write-Host "Saldo disponível: $($wallet.available_wc) WC."

# ------------------------------------------------------------------ 5. pedido real
Write-Step "Pedido real"
$names = ($ready | ForEach-Object { $_.model_identifier }) -join ", "
Write-Host "Vai ser enviada UMA mensagem curta a: $names"
Write-Host "Isto é um pedido real, pago pelo saldo pré-pago do fornecedor (fração de cêntimo)."
$answer = Read-Host "Continuar? (s/n)"
if ($answer -notmatch '^[sSyY]') { Write-Host "Cancelado. Nada foi enviado."; return }

foreach ($model in $ready) {
    $message = "Ola! Responde numa unica frase curta: qual e a capital de Portugal?"
    $estimate = Invoke-Api POST "/ai/estimate" @{ model_id = $model.id; message = $message }
    Write-Host ""
    Write-Host "$($model.model_identifier): reserva máxima $($estimate.worst_case.credits_wc) WC; a enviar..."
    $result = Invoke-Api POST "/ai/chat" @{
        request_id = [guid]::NewGuid().ToString(); model_id = $model.id
        message = $message; stream = $false
    }
    if ($result.outcome -eq "usage") {
        $r = $result.result
        Write-Host "Resposta: $($result.text)" -ForegroundColor Green
        Write-Host ("Tokens: {0} entrada, {1} saída | custo {2} {3} = {4} EUR | cobrado {5} WC | saldo {6} WC" -f `
            ($r.tokens.input + $r.tokens.cached_input), $r.tokens.output, $r.provider_cost, $r.currency,
            $r.cost_eur, $r.charged_wc, $r.wallet.available_wc)
    } else {
        Write-Host "Falhou: $($result.result.code) - $($result.result.detail)" -ForegroundColor Red
        Write-Host "Cobrado: $($result.result.charged_wc) WC"
    }
}
Write-Host ""
Write-Host "Concluído. Veja a conversa em GET /ai/sessions e o registo em GET /wallet/history."
