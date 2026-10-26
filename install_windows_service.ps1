<#
Registers Aquarius Bot Manager as a Windows service via NSSM, matching the
MelonBank precedent on this box (NSSM-style wrapper, auto-restart, auto-start
at boot) and replacing the Linux deployment's two systemd units:
  - aquarius-bot-manager.service      -> this NSSM service
  - aquarius-bot-manager-boot.service -> inlined into serve()'s own startup
    path on Windows (see manager.py's IS_WINDOWS branch in serve()), since
    Windows services have no oneshot-at-boot idiom to run it separately.

Boot mode (confirmed in the approved plan): an install-TIME choice, not a
runtime setting --
  -BootMode Always     : Start=SERVICE_AUTO_START, the service starts at
                          boot with no user logged in.
  -BootMode WaitForLogin: Start=SERVICE_DEMAND_START plus a scheduled task
                          that starts the service at the FIRST interactive
                          logon on this machine (any user) and never again
                          after that -- matching "wait for a user to log in
                          once, then behave like an always-on service" rather
                          than restarting itself every single logon.

Requires: NSSM on PATH (winget install NSSM.NSSM), and must be run elevated
(service installation requires admin rights).

Usage:
    .\install_windows_service.ps1 -BootMode Always
    .\install_windows_service.ps1 -BootMode WaitForLogin
#>

param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("Always", "WaitForLogin")]
    [string]$BootMode,

    [string]$ServiceName = "AquariusBotManager",
    [string]$PythonExe,
    [string]$ManagerDir = $PSScriptRoot,
    [string]$ConfigPath = (Join-Path $PSScriptRoot "instances.json"),
    [string]$BindHost = "127.0.0.1",
    [int]$Port = 8765
)

function Resolve-RealPython {
    # `Get-Command python.exe` can resolve to the Microsoft Store
    # app-execution-alias stub under WindowsApps, which is NOT a real
    # interpreter (it just prompts a Store install when run) -- confirmed
    # the hard way earlier in this same install. Explicitly skip that path
    # and require a real python.exe elsewhere on PATH.
    $candidates = Get-Command python.exe -All -ErrorAction SilentlyContinue |
        Where-Object { $_.Source -notlike "*\WindowsApps\python.exe" }
    if ($candidates) { return $candidates[0].Source }
    throw "No real python.exe found on PATH (only the Microsoft Store alias stub, if anything). " +
          "Install Python (e.g. 'winget install Python.Python.3.12') and pass -PythonExe explicitly if needed."
}

if (-not $PythonExe) {
    $PythonExe = Resolve-RealPython
}

$nssm = (Get-Command nssm.exe -ErrorAction Stop).Source
$taskName = "$ServiceName-LoginStart"

Write-Host "Installing service '$ServiceName' (boot mode: $BootMode)..."

$appParameters = "manager.py --config `"$ConfigPath`" serve --host $BindHost --port $Port"

# `nssm install` fails outright if the service already exists (e.g. re-
# running this script to switch boot modes on an existing install) --
# update it in place via `nssm set` instead of reinstalling.
$existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Service already exists; updating its configuration in place."
    & $nssm set $ServiceName Application $PythonExe
    & $nssm set $ServiceName AppParameters $appParameters
} else {
    & $nssm install $ServiceName $PythonExe $appParameters
}
& $nssm set $ServiceName AppDirectory $ManagerDir
& $nssm set $ServiceName AppStdout (Join-Path $ManagerDir "service-stdout.log")
& $nssm set $ServiceName AppStderr (Join-Path $ManagerDir "service-stderr.log")
# Auto-restart on crash, matching MelonBank's --maxrestarts/--wait pattern:
# NSSM's own throttle is a reset window rather than a hard attempt count, so
# this is the closest equivalent -- restart after 2s, and NSSM's default
# "give up if it dies again within 1500ms" guard prevents a true crash loop
# from spinning without any backoff at all.
& $nssm set $ServiceName AppRestartDelay 2000
& $nssm set $ServiceName AppThrottle 1500

# Clean up any previous run's login-start task before (re)applying the mode,
# so switching modes on an existing install doesn't leave a stale task behind.
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue

if ($BootMode -eq "Always") {
    & $nssm set $ServiceName Start SERVICE_AUTO_START
    Write-Host "Service will start at boot, before any user logs in."
} else {
    & $nssm set $ServiceName Start SERVICE_DEMAND_START
    $action = New-ScheduledTaskAction -Execute "sc.exe" -Argument "start $ServiceName"
    $trigger = New-ScheduledTaskTrigger -AtLogOn
    $principal = New-ScheduledTaskPrincipal -GroupId "BUILTIN\Users" -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings -Description `
        "Starts the $ServiceName service at the first user logon after boot (wait-for-login boot mode)." | Out-Null
    Write-Host "Service will stay stopped at boot and start at the first user logon."
}

Write-Host "`nDone. Manage it with:"
Write-Host "  net start $ServiceName"
Write-Host "  net stop $ServiceName"
Write-Host "  Get-Service $ServiceName"
