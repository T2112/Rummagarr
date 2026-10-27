$ErrorActionPreference = "Stop"
foreach ($taskName in @("RummagarrWorker", "LidarrSoulseekWorker")) {
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
}
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match "main.py" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Write-Host "Stopped and removed scheduled tasks RummagarrWorker and LidarrSoulseekWorker, if they were present."
Write-Host "config.toml and downloaded music were not deleted."
