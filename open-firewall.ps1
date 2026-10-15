# Run PowerShell as Administrator.
# Opens the Soulseek listen ports and the Lidarr bridge port.
# The installed app keeps config.toml in %LOCALAPPDATA%\Rummagarr.
# A script install keeps it beside this file. Missing config uses
# 2234, 2235, and -BridgePort (default 8788).
param(
    [int]$BridgePort = 8788
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path

function Get-TomlInt([string]$text, [string]$key, [int]$default) {
    if ($text -match "(?m)^\s*$key\s*=\s*(\d+)") { return [int]$Matches[1] }
    return $default
}

function Get-BridgePort([string]$text, [int]$default) {
    if ($text -match '(?s)\[bridge\].*?(?m)^\s*port\s*=\s*(\d+)') { return [int]$Matches[1] }
    return $default
}

$configPath = Join-Path $env:LOCALAPPDATA "Rummagarr\config.toml"
if (-not (Test-Path $configPath)) {
    $configPath = Join-Path $Root "config.toml"
}

$listen = 2234
$obfs = 2235
if (Test-Path $configPath) {
    $toml = Get-Content $configPath -Raw
    $listen = Get-TomlInt $toml "listen_port" 2234
    $obfs = Get-TomlInt $toml "obfuscated_port" 2235
    $BridgePort = Get-BridgePort $toml $BridgePort
}

foreach ($port in @($listen, $obfs)) {
    $name = "Rummagarr TCP $port"
    netsh advfirewall firewall delete rule name=$name | Out-Null
    netsh advfirewall firewall add rule name=$name dir=in action=allow protocol=TCP localport=$port
    Write-Host "Allowed inbound TCP $port"
}

$bridgeName = "Rummagarr Bridge $BridgePort"
netsh advfirewall firewall delete rule name="$bridgeName" | Out-Null
netsh advfirewall firewall add rule name="$bridgeName" dir=in action=allow protocol=TCP localport=$BridgePort
Write-Host "Allowed inbound TCP $BridgePort for the Lidarr bridge"

$exe = Join-Path $Root "Rummagarr.exe"
if (Test-Path $exe) {
    netsh advfirewall firewall delete rule name="Rummagarr" | Out-Null
    netsh advfirewall firewall add rule name="Rummagarr" dir=in action=allow program="$exe" enable=yes
    Write-Host "Allowed $exe"
}

Write-Host "If this PC is behind a router, also forward TCP $listen and $obfs to this machine."
Write-Host "Lidarr on another PC uses this PC's Tailscale address and bridge port $BridgePort."
