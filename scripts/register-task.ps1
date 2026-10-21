param(
    [string]$Python = (Get-Command python -ErrorAction Stop).Source,
    [string]$Config = (Join-Path $PSScriptRoot '..\inventory.config.json'),
    [int]$IntervalMinutes = 10,
    [string]$TaskName = 'Agent Inventory MVP',
    [switch]$StartNow,
    [switch]$UpdateExisting
)
$ErrorActionPreference = 'Stop'
if ($IntervalMinutes -lt 1) { throw 'IntervalMinutes must be positive' }
$Python = (Get-Command $Python -ErrorAction Stop).Source
$configPath = (Resolve-Path $Config).Path
& $Python -c 'import sys, psutil; print(sys.executable)'
if ($LASTEXITCODE -ne 0) { throw 'Install requirements.txt with the selected Python before registering.' }
$userName = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$pythonWindowless = Join-Path (Split-Path -Parent $Python) 'pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonWindowless)) { throw 'The selected Python must provide pythonw.exe for hidden execution.' }
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$arguments = '-m agent_inventory.scheduled --config "{0}"' -f $configPath
$action = New-ScheduledTaskAction -Execute $pythonWindowless -Argument $arguments -WorkingDirectory $repoRoot
$periodic = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)
$logon = New-ScheduledTaskTrigger -AtLogOn -User $userName
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
$principal = New-ScheduledTaskPrincipal -UserId $userName -LogonType Interactive -RunLevel Limited
$description = 'Agent Inventory: scan on logon and periodically while the user is logged on.'
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    if (-not $UpdateExisting) { throw 'Task already exists. Use -UpdateExisting to update this managed task.' }
    $currentIdentity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
    $allowedUsers = @($userName, $userName.Split('\')[-1], $currentIdentity.User.Value)
    if ($existing.Description -ne $description -or $existing.Principal.UserId -notin $allowedUsers) {
        throw 'Refusing to replace an unrelated task or another user task.'
    }
}
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger @($periodic,$logon) -Settings $settings -Principal $principal -Description $description -Force:([bool]$existing)
if ($StartNow) { Start-ScheduledTask -TaskName $TaskName }
