# Run PowerShell as Administrator.
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$configPath = Join-Path $Root "config.toml"
if (-not (Test-Path $configPath)) {
    Write-Error "config.toml not found. Copy config.example.toml first."
}

function Get-TomlInt([string]$text, [string]$key, [int]$default) {
    if ($text -match "(?m)^\s*$key\s*=\s*(\d+)") { return [int]$Matches[1] }
    return $default
}

$toml = Get-Content $configPath -Raw
$listen = Get-TomlInt $toml "listen_port" 2234
$obfs = Get-TomlInt $toml "obfuscated_port" 2235

foreach ($port in @($listen, $obfs)) {
    $name = "Lidarr Soulseek TCP $port"
    netsh advfirewall firewall delete rule name=$name | Out-Null
    netsh advfirewall firewall add rule name=$name dir=in action=allow protocol=TCP localport=$port
    Write-Host "Allowed inbound TCP $port"
}

Write-Host "If this PC is behind a router, also forward TCP $listen and $obfs to this machine."
