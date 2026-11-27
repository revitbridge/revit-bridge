param([Parameter(Mandatory=$true)][string]$Path)
$ErrorActionPreference='Stop'
$resolved=(Resolve-Path -LiteralPath $Path).Path
$installer=New-Object -ComObject WindowsInstaller.Installer
$database=$installer.OpenDatabase($resolved,0)
$properties=[ordered]@{}
$propertyView=$database.OpenView('SELECT `Property`,`Value` FROM `Property`')
[void]$propertyView.Execute()
while ($record=$propertyView.Fetch()) {
    $name=$record.StringData(1)
    if ($name -in @('ProductCode','ProductVersion','ProductName','Manufacturer','RVT_S_CONTENTPATH','CONTENT_PATH')) { $properties[$name]=$record.StringData(2) }
}
[void]$propertyView.Close()
$fileView=$database.OpenView('SELECT `FileName`,`FileSize` FROM `File`')
[void]$fileView.Execute()
$counts=[ordered]@{Rfa=0;Rft=0;Rte=0;Other=0;Total=0;Bytes=[long]0}
while ($record=$fileView.Fetch()) {
    $longName=($record.StringData(1) -split '\|')[-1]
    $counts.Total++
    $counts.Bytes += $record.IntegerData(2)
    switch ([IO.Path]::GetExtension($longName).ToLowerInvariant()) {
        '.rfa' {$counts.Rfa++}
        '.rft' {$counts.Rft++}
        '.rte' {$counts.Rte++}
        default {$counts.Other++}
    }
}
[void]$fileView.Close()
[pscustomobject]@{MsiPath=$resolved;Properties=$properties;FileCounts=$counts} | ConvertTo-Json -Depth 4
