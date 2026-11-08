# Lists every running Revit.exe: pid, year (from the exe path), start time, parent pid,
# whether it responds and its main window title, so you can tell your own test Revit
# from someone else's. Read-only.
# -RequireNone: exit 3 when any Revit runs (use it before installing or launching).
# Usage: powershell -NoProfile -File revit_ps.ps1 [-RequireNone]
param([switch] $RequireNone)
# UTF-8 output so Git Bash and Python read non-ASCII window titles correctly.
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$procs = @(Get-CimInstance Win32_Process -Filter "Name='Revit.exe'")
if ($procs.Count -eq 0) { Write-Output 'no Revit running'; exit 0 }
foreach ($p in $procs) {
    $year = if ($p.ExecutablePath -match 'Revit (\d{4})') { $Matches[1] } else { '?' }
    $title = ''
    $responding = ''
    try {
        $gp = Get-Process -Id $p.ProcessId -ErrorAction Stop
        $title = $gp.MainWindowTitle
        $responding = $gp.Responding
    } catch {}
    Write-Output ("pid={0} year={1} started={2:yyyy-MM-dd HH:mm:ss} parent={3} responding={4} title=[{5}] args={6}" -f `
        $p.ProcessId, $year, $p.CreationDate, $p.ParentProcessId, $responding, $title, ($p.CommandLine -replace '^"[^"]*"\s*', ''))
}
if ($RequireNone) { Write-Output 'STOP: a Revit is running - do not install or launch'; exit 3 }
exit 0
