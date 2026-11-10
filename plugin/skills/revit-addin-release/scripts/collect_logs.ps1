# Collect what a Revit add-in's developer needs from a user's machine into one
# zip on the Desktop: where the add-in is registered, which DLL each
# registration points at (version, date, hash), the Revit releases installed
# and running, the recent journals, and the add-in's own log files.
#
# The developer fills the add-in name into collect_logs.bat and sends both
# files to the user, who double-clicks the .bat. Direct use:
#   powershell -NoProfile -ExecutionPolicy Bypass -File collect_logs.ps1 -Name <Name>
#       [-Days 7] [-LogPath <file or glob> ...] [-OutDir <dir>] [-NoExplorer]
#
# <Name> is the add-in name as it appears in <Name>.addin, <Name>.dll and
# <Name>.bundle. -LogPath adds the add-in's own log files, for example
# "%LOCALAPPDATA%\<Name>\log\*.log"; %LOCALAPPDATA%\<Name> and %APPDATA%\<Name>
# are looked at by default.
#
# Read-only: nothing on the machine is changed and no process is touched.
# Files whose names suggest credentials (token, secret, password, credential,
# identity) are listed but never copied. Journals contain file names and the
# Windows account name; tell the user.
param(
    [string]$Name = '',
    [int]$Days = 7,
    [string[]]$LogPath = @(),
    [string]$OutDir = [Environment]::GetFolderPath('Desktop'),
    [switch]$NoExplorer
)
$ErrorActionPreference = 'Continue'
$ScriptVersion = '2026-11-09'

if (-not $Name -or $Name -eq 'REPLACE-ME') {
    Write-Host 'collect_logs.ps1: the add-in name is missing. Open collect_logs.bat and replace REPLACE-ME with the add-in name.'
    exit 2
}

$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$bundleName = "$Name-logs-$($env:COMPUTERNAME)-$stamp"
$work = Join-Path $env:TEMP $bundleName
$files = Join-Path $work 'files'
New-Item -ItemType Directory -Force $files | Out-Null
$since = (Get-Date).AddDays(-$Days)
$report = New-Object System.Collections.Generic.List[string]
$secretLike = '(?i)token|secret|password|credential|identity'
$namePattern = [regex]::Escape($Name)

function Say([string]$text) { $report.Add($text) }
function Section([string]$title) { Say ''; Say "=== $title ===" }
function Try-Do([scriptblock]$block, [string]$what) {
    try { & $block } catch { Say "  [error] $what : $($_.Exception.Message)" }
}

# Copy one file into files\<label>\ unless it looks like a credential. A name already
# taken there (the same file name from another folder) gets ~2, ~3, ... before its
# extension, so no copy overwrites another; the report names the copy as it is in the zip.
function Grab([string]$path, [string]$label) {
    if (-not (Test-Path -LiteralPath $path)) { return }
    $item = Get-Item -LiteralPath $path
    if ($item.Name -match $secretLike) {
        Say ("  skipped {0} ({1} bytes): name suggests a credential" -f $path, $item.Length)
        return
    }
    $dest = Join-Path $files $label
    New-Item -ItemType Directory -Force $dest | Out-Null
    $leaf = $item.Name
    $n = 1
    while (Test-Path -LiteralPath (Join-Path $dest $leaf)) {
        $n++
        $leaf = '{0}~{1}{2}' -f $item.BaseName, $n, $item.Extension
    }
    try {
        Copy-Item -LiteralPath $path -Destination (Join-Path $dest $leaf) -Force -ErrorAction Stop
        Say ("  copied {0} ({1} bytes, modified {2:yyyy-MM-dd HH:mm:ss}) -> files\{3}\{4}" -f `
            $path, $item.Length, $item.LastWriteTime, $label, $leaf)
    } catch {
        Say ("  copy FAILED {0} ({1} bytes): {2}" -f $path, $item.Length, $_.Exception.Message.Trim())
    }
}

# An XML file read the way Revit reads it: the BOM or the XML declaration picks the
# encoding, UTF-8 without either. Get-Content would decode a BOM-less UTF-8 file with
# the ANSI code page under Windows PowerShell 5.1 (GBK on a Chinese Windows) and garble
# a path such as C:\Users\<Chinese name>\...
function Read-Xml([string]$path) {
    $doc = New-Object System.Xml.XmlDocument
    $doc.XmlResolver = $null
    $doc.Load($path)
    return ,$doc
}

function Reg-Value([string]$key, [string]$value) {
    try { (Get-ItemProperty -LiteralPath $key -ErrorAction Stop).$value } catch { $null }
}

# Flags set inside ForEach-Object blocks use $script: scope; a plain assignment there
# would create a block-local variable and the "nothing found" lines would print wrongly.

# One line per DLL: version, date, size, start of the SHA-256.
function Describe-Dll([string]$path, [string]$indent) {
    if (-not (Test-Path -LiteralPath $path)) { Say "$indent dll MISSING $path"; return }
    $d = Get-Item -LiteralPath $path
    $hash = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.Substring(0, 12)
    Say ("{0} dll {1}  version={2}  modified={3:yyyy-MM-dd HH:mm}  size={4}  sha256={5}" -f `
        $indent, $path, $d.VersionInfo.FileVersion, $d.LastWriteTime, $d.Length, $hash)
}

Write-Host "Collecting $Name logs, please wait..."

Section 'machine'
Say "  collected   $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss zzz') (script $ScriptVersion, last $Days days, add-in $Name)"
Say "  computer    $($env:COMPUTERNAME)"
Say "  windows user $($env:USERDOMAIN)\$($env:USERNAME)"
Try-Do {
    $nt = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion'
    Say "  os          $(Reg-Value $nt 'ProductName') $(Reg-Value $nt 'DisplayVersion') build $(Reg-Value $nt 'CurrentBuildNumber') ($([Environment]::OSVersion.Version))"
} 'os'

# --- Revit ------------------------------------------------------------------
Section 'Revit installed'
# Revit records each installation under HKLM\SOFTWARE\Autodesk\Revit\<year>\REVIT-xx:xxxx
# (ProductName, Version, InstallationLocation, Language); the folder name alone does
# not say whether it is a .5 release, so the Revit.exe file version is read as well.
$revitFound = $false
Try-Do {
    Get-ChildItem -LiteralPath 'HKLM:\SOFTWARE\Autodesk\Revit' -ErrorAction Stop | ForEach-Object {
        Get-ChildItem -LiteralPath $_.PSPath -ErrorAction SilentlyContinue |
            Where-Object { $_.PSChildName -like 'REVIT-*' } | ForEach-Object {
                if (-not (Reg-Value $_.PSPath 'ProductName')) { return }     # empty leftover key
                $script:revitFound = $true
                $location = Reg-Value $_.PSPath 'InstallationLocation'
                $exeVersion = '(no Revit.exe)'
                if ($location) {
                    $exe = Join-Path $location 'Revit.exe'
                    if (Test-Path -LiteralPath $exe) { $exeVersion = (Get-Item -LiteralPath $exe).VersionInfo.FileVersion }
                }
                Say ("  installed   {0}  registry version {1}  Revit.exe {2}  language {3}  at {4}" -f `
                    (Reg-Value $_.PSPath 'ProductName'), (Reg-Value $_.PSPath 'Version'), $exeVersion, (Reg-Value $_.PSPath 'Language'), $location)
            }
    }
} 'installed Revit (registry)'
if (-not $revitFound) {
    Try-Do {
        Get-ChildItem -LiteralPath (Join-Path $env:ProgramFiles 'Autodesk') -Directory -Filter 'Revit 20*' -ErrorAction Stop |
            ForEach-Object {
                $exe = Join-Path $_.FullName 'Revit.exe'
                if (Test-Path -LiteralPath $exe) {
                    $script:revitFound = $true
                    Say "  installed   $($_.Name)  Revit.exe $((Get-Item -LiteralPath $exe).VersionInfo.FileVersion)  at $($_.FullName)"
                }
            }
    } 'installed Revit (Program Files)'
}
if (-not $revitFound) { Say '  installed   (no Revit found in the registry or under Program Files\Autodesk)' }

Section 'Revit running'
Try-Do {
    $procs = Get-CimInstance Win32_Process -Filter "Name='Revit.exe'"
    if ($procs) {
        foreach ($p in $procs) {
            Say "  running     pid=$($p.ProcessId) started=$($p.CreationDate.ToString('yyyy-MM-dd HH:mm:ss')) path=$($p.ExecutablePath) cmd=$($p.CommandLine)"
        }
    } else { Say '  running     (no Revit.exe)' }
} 'running Revit'

# --- uninstall entries ------------------------------------------------------
Section 'uninstall entries'
$anyUninstall = $false
foreach ($root in 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall',
                  'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall',
                  'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall') {
    Try-Do {
        Get-ChildItem -LiteralPath $root -ErrorAction Stop | ForEach-Object {
            $display = Reg-Value $_.PSPath 'DisplayName'
            if ($display -and $display -match $namePattern) {
                $script:anyUninstall = $true
                Say ("  {0}  version={1}  installed={2}  location={3}" -f `
                    ($_.Name -replace '^HKEY_LOCAL_MACHINE', 'HKLM' -replace '^HKEY_CURRENT_USER', 'HKCU'), `
                    (Reg-Value $_.PSPath 'DisplayVersion'), (Reg-Value $_.PSPath 'InstallDate'), (Reg-Value $_.PSPath 'InstallLocation'))
            }
        }
    } "uninstall $root"
}
if (-not $anyUninstall) { Say "  (no uninstall entry whose DisplayName contains $Name)" }

# --- classic manifests ------------------------------------------------------
Section 'classic manifests (.addin)'
$anyManifest = $false
$manifestRoots = @(
    @{ scope = 'machine'; path = (Join-Path $env:ProgramData 'Autodesk\Revit\Addins') },
    @{ scope = 'user'; path = (Join-Path $env:APPDATA 'Autodesk\Revit\Addins') })
foreach ($root in $manifestRoots) {
    if (-not (Test-Path -LiteralPath $root.path)) { Say "  $($root.scope): (no $($root.path))"; continue }
    Get-ChildItem -LiteralPath $root.path -Directory -ErrorAction SilentlyContinue | ForEach-Object {
        $year = $_.Name
        Get-ChildItem -LiteralPath $_.FullName -Filter '*.addin' -File -ErrorAction SilentlyContinue | ForEach-Object {
            $manifest = $_
            $mentions = $manifest.Name -match $namePattern
            if (-not $mentions) {
                $mentions = [bool](Select-String -LiteralPath $manifest.FullName -Pattern $namePattern -Quiet -ErrorAction SilentlyContinue)
            }
            if (-not $mentions) { return }
            $script:anyManifest = $true
            Say ("  {0} {1}: {2}  modified={3:yyyy-MM-dd HH:mm}" -f $root.scope, $year, $manifest.FullName, $manifest.LastWriteTime)
            Grab $manifest.FullName ("addins\" + $root.scope + "\" + $year)
            Try-Do {
                $xml = Read-Xml $manifest.FullName
                foreach ($node in $xml.SelectNodes('//Assembly')) {
                    $dll = $node.InnerText.Trim()
                    if (-not [System.IO.Path]::IsPathRooted($dll)) { $dll = Join-Path $manifest.DirectoryName $dll }
                    Describe-Dll $dll '     '
                }
                foreach ($id in $xml.SelectNodes('//AddInId')) { Say "      AddInId $($id.InnerText.Trim())" }
            } "parse $($manifest.FullName)"
        }
    }
}
if (-not $anyManifest) { Say "  (no .addin naming $Name in the machine or user Addins folders)" }

Section 'manifests in other user profiles (not copied)'
Try-Do {
    $script:others = 0
    Get-ChildItem -LiteralPath (Join-Path $env:SystemDrive 'Users') -Directory -ErrorAction Stop | ForEach-Object {
        if ($_.Name -eq $env:USERNAME) { return }
        $addins = Join-Path $_.FullName 'AppData\Roaming\Autodesk\Revit\Addins'
        if (-not (Test-Path -LiteralPath $addins)) { return }
        Get-ChildItem -LiteralPath $addins -Recurse -Filter '*.addin' -File -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -match $namePattern } |
            ForEach-Object { $script:others++; Say ("  {0}  modified={1:yyyy-MM-dd HH:mm}" -f $_.FullName, $_.LastWriteTime) }
    }
    if ($script:others -eq 0) { Say '  (none readable)' }
} 'other profiles'

# --- bundles ----------------------------------------------------------------
Section 'autoloader bundles'
$anyBundle = $false
foreach ($pluginsDir in (Join-Path $env:APPDATA 'Autodesk\ApplicationPlugins'),
                        (Join-Path $env:ProgramData 'Autodesk\ApplicationPlugins'),
                        (Join-Path $env:ProgramFiles 'Autodesk\ApplicationPlugins')) {
    if (-not (Test-Path -LiteralPath $pluginsDir)) { continue }
    Get-ChildItem -LiteralPath $pluginsDir -Directory -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match $namePattern } | ForEach-Object {
            $script:anyBundle = $true
            Say "  bundle      $($_.FullName)"
            $pc = Join-Path $_.FullName 'PackageContents.xml'
            if (Test-Path -LiteralPath $pc) {
                Try-Do {
                    $xml = Read-Xml $pc
                    Say "    AppVersion $($xml.ApplicationPackage.AppVersion)"
                } 'PackageContents.xml'
                Grab $pc ("bundles\" + $_.Name)
            }
            Get-ChildItem -LiteralPath $_.FullName -Recurse -Filter '*.dll' -File -ErrorAction SilentlyContinue |
                Where-Object { $_.Name -match $namePattern } |
                ForEach-Object { Describe-Dll $_.FullName '   ' }
        }
}
if (-not $anyBundle) { Say "  (no bundle naming $Name in any ApplicationPlugins folder)" }

# --- journals ---------------------------------------------------------------
Section 'journals'
$journalKeys = @('Starting External Application', 'Duplicate addins', 'AddInLoadFailureMessage',
                 'Assembly version conflict', "won't be loaded", $Name)
$revitLocal = Join-Path $env:LOCALAPPDATA 'Autodesk\Revit'
if (Test-Path -LiteralPath $revitLocal) {
    Get-ChildItem -LiteralPath $revitLocal -Directory -Filter 'Autodesk Revit 20*' -ErrorAction SilentlyContinue | ForEach-Object {
        $label = $_.Name
        $journals = Join-Path $_.FullName 'Journals'
        if (-not (Test-Path -LiteralPath $journals)) { return }
        $recent = Get-ChildItem -LiteralPath $journals -File -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -like 'journal.*' -and $_.LastWriteTime -ge $since } |
            Sort-Object LastWriteTime
        if (-not $recent) { Say "  $label : no journal modified in the last $Days days"; return }
        $newest = $recent | Where-Object { $_.Extension -eq '.txt' } | Select-Object -Last 1
        if ($newest) {
            # Revit writes its journals in the ANSI code page (GBK on a Chinese Windows), which
            # is what Get-Content assumes for a file without a BOM under Windows PowerShell 5.1;
            # the header lines matched here are ASCII, the same under any decoding.
            Try-Do {
                Get-Content -LiteralPath $newest.FullName -TotalCount 60 -ErrorAction Stop |
                    Where-Object { $_ -match 'Build:|Release:|Branch:' } |
                    ForEach-Object { Say "  $label header: $($_.Trim())" }
            } "header of $($newest.Name)"
        }
        foreach ($j in $recent) {
            Grab $j.FullName ("journals\" + $label)
            if ($j.Extension -ne '.txt') { continue }
            Try-Do {
                $counts = foreach ($key in $journalKeys) {
                    $n = (Select-String -LiteralPath $j.FullName -SimpleMatch -Pattern $key -ErrorAction SilentlyContinue | Measure-Object).Count
                    "$key=$n"
                }
                Say "    $($j.Name): $($counts -join ', ')"
            } "scan $($j.Name)"
        }
    }
} else { Say "  (no $revitLocal)" }

# --- the add-in's own files -------------------------------------------------
Section 'add-in logs (-LogPath)'
if ($LogPath.Count -eq 0) { Say '  (no -LogPath given)' }
foreach ($pattern in $LogPath) {
    $expanded = [Environment]::ExpandEnvironmentVariables($pattern)
    Try-Do {
        $matches = Get-ChildItem -Path $expanded -File -ErrorAction Stop | Where-Object { $_.LastWriteTime -ge $since }
        if (-not $matches) { Say "  $expanded : nothing modified in the last $Days days" }
        foreach ($m in $matches) { Grab $m.FullName 'addin-logs' }
    } "log path $expanded"
}

Section 'add-in data folders'
foreach ($dir in (Join-Path $env:LOCALAPPDATA $Name), (Join-Path $env:APPDATA $Name)) {
    if (-not (Test-Path -LiteralPath $dir)) { Say "  (no $dir)"; continue }
    Say "  $dir"
    Get-ChildItem -LiteralPath $dir -Recurse -File -ErrorAction SilentlyContinue | ForEach-Object {
        Say ("    {0}  {1} bytes  modified={2:yyyy-MM-dd HH:mm}" -f $_.FullName.Substring($dir.Length + 1), $_.Length, $_.LastWriteTime)
        if ($_.LastWriteTime -ge $since -and $_.Extension -match '^\.(log|txt|json|cfg|ini)$') {
            Grab $_.FullName ("addin-data\" + (Split-Path -Leaf $dir))
        }
    }
}

# --- pack -------------------------------------------------------------------
Set-Content -LiteralPath (Join-Path $work 'report.txt') -Value $report -Encoding UTF8
$zip = Join-Path $OutDir "$bundleName.zip"
try {
    Compress-Archive -Path (Join-Path $work '*') -DestinationPath $zip -Force -ErrorAction Stop
    Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue
    Write-Host ''
    Write-Host "Done. Send this file to the $Name developer:"
    Write-Host "  $zip"
    if (-not $NoExplorer) { Start-Process explorer.exe "/select,`"$zip`"" -ErrorAction SilentlyContinue }
} catch {
    Write-Host "Could not create the zip ($($_.Exception.Message)). Send this folder instead:"
    Write-Host "  $work"
}
