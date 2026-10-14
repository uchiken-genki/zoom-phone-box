# ngrok を起動し、公開 URL と転送先ポートをアプリの data/ へ書き込む。
# フリー版は起動のたびに FQDN が変わる。既に待受ポートへ転送しているトンネルがあれば再利用する。
# 実行ファイルは環境変数 NGROK_EXE、または PATH 上の ngrok を使う。
$ErrorActionPreference = "Stop"

$Port = 8787
if ($env:APP_PORT -match '^\d+$') {
  $Port = [int]$env:APP_PORT
}
$DataDir = Join-Path $PSScriptRoot "data"
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null

function Resolve-Ngrok {
  if ($env:NGROK_EXE -and (Test-Path -LiteralPath $env:NGROK_EXE)) {
    return $env:NGROK_EXE
  }
  $cmd = Get-Command ngrok -ErrorAction SilentlyContinue
  if ($cmd) {
    return $cmd.Source
  }
  Write-Error "ngrok が見つかりません。PATH に通すか、環境変数 NGROK_EXE に実行ファイルのパスを設定してください。"
}

function Get-AppTunnel {
  try {
    $payload = Invoke-RestMethod -Uri "http://127.0.0.1:4040/api/tunnels" -TimeoutSec 2
  } catch {
    return $null
  }
  foreach ($tunnel in @($payload.tunnels)) {
    $publicUrl = [string]$tunnel.public_url
    if ($tunnel.proto -ne "https" -or $publicUrl -notmatch '^https://') { continue }
    $addr = [string]$tunnel.config.addr
    if ($addr -match ":$Port$" -or $addr -eq "$Port") {
      return $tunnel
    }
  }
  return $null
}

function Save-AppTunnel($Tunnel) {
  $publicUrl = ([string]$Tunnel.public_url).TrimEnd("/")
  if ($publicUrl -notmatch '^https://') {
    Write-Error "ngrok の公開 URL が空です。トンネルの起動を確認してください。"
  }
  $uri = [Uri]$publicUrl
  $publicPort = if ($uri.Port -gt 0) { $uri.Port } elseif ($uri.Scheme -eq "https") { 443 } else { 80 }
  $utf8 = New-Object System.Text.UTF8Encoding $false
  [System.IO.File]::WriteAllText((Join-Path $DataDir "public_base_url.txt"), $publicUrl + "`n", $utf8)
  $meta = @{
    public_url  = $publicUrl
    public_port = $publicPort
    local_port  = $Port
  } | ConvertTo-Json
  [System.IO.File]::WriteAllText((Join-Path $DataDir "ngrok.json"), $meta + "`n", $utf8)
  Write-Host "ngrok $publicUrl -> localhost:${Port}"
  Write-Host "Webhook URL: $publicUrl/zoom/webhook"
}

$Ngrok = Resolve-Ngrok
$tunnel = Get-AppTunnel
if (-not $tunnel) {
  $running = Get-Process -Name ngrok -ErrorAction SilentlyContinue
  if ($running) {
    Write-Host "別ポートの ngrok を止めて、localhost:${Port} へ向け直します。"
    Stop-Process -Name ngrok -Force
    Start-Sleep -Seconds 1
  }
  Start-Process -FilePath $Ngrok -ArgumentList @("http", "$Port")
  $deadline = (Get-Date).AddSeconds(25)
  do {
    Start-Sleep -Milliseconds 500
    $tunnel = Get-AppTunnel
  } while (-not $tunnel -and (Get-Date) -lt $deadline)
}

if (-not $tunnel) {
  Write-Error "ngrok の公開 URL を取得できませんでした。ngrok http $Port を確認してください。"
}

Save-AppTunnel $tunnel
