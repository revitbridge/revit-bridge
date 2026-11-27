<#
.SYNOPSIS
Reads a signed Autodesk Revit CHS/ENU content bundle and proposes a filename.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Path,
    [string]$SevenZip
)
$ErrorActionPreference = 'Stop'
$file = Get-Item -LiteralPath $Path
if ($file.PSIsContainer -or $file.Extension -ne '.exe') {
    throw 'Select a Revit content pack EXE.'
}
$signature = Get-AuthenticodeSignature -LiteralPath $file.FullName
if ($signature.Status -ne 'Valid') {
    throw ('Invalid Authenticode signature: ' + $signature.Status)
}
$signer = $signature.SignerCertificate.GetNameInfo(
    [System.Security.Cryptography.X509Certificates.X509NameType]::SimpleName, $false
)
if ($signer -notmatch '^Autodesk,? Inc\.?$') {
    throw ('Unexpected signer: ' + $signer)
}
if (-not $SevenZip) {
    $command = Get-Command '7z.exe' -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($command) { $SevenZip = $command.Source }
    else { $SevenZip = Join-Path $env:ProgramFiles '7-Zip\7z.exe' }
}
$reader = Get-Item -LiteralPath $SevenZip
$manifest = & $reader.FullName e -so -spd -- $file.FullName 'setup.xml'
if ($LASTEXITCODE -ne 0 -or -not $manifest) { throw 'Cannot read root setup.xml.' }
$xml = New-Object System.Xml.XmlDocument
$xml.XmlResolver = $null
$xml.LoadXml(($manifest -join [Environment]::NewLine))
$identity = $xml.SelectSingleNode('/*[local-name()="Bundle"]/*[local-name()="Identity"]')
if (-not $identity -or $identity.PLC -ne 'RVT' -or $identity.UpdateType -ne 'CP') {
    throw 'Expected a Revit content pack in the root manifest.'
}
$year = [string]$identity.Release
$title = [string]$identity.DisplayName
if ($year -notmatch '^20\d\d$' -or $title -notmatch "\b$year\b") { throw 'Inconsistent release year.' }
if ($title -match '^(?:Autodesk )?US English Content for (?:Autodesk )?Revit\b') { $locale = 'ENU' }
elseif ($title -match '^Chinese Content for (?:Autodesk )?Revit\b') { $locale = 'CHS' }
else { throw ('Inspect this locale separately: ' + $title) }
$build = $identity.SelectSingleNode('*[local-name()="BuildNumber"]')
if (-not $build -or -not $build.InnerText.Trim()) { throw 'Missing bundle build number.' }
[pscustomobject]@{
    File = $file.FullName
    Bytes = $file.Length
    Sha256 = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash
    Signer = $signer
    Year = [int]$year
    Locale = $locale
    Title = $title
    Build = $build.InnerText
    VerifiedName = "RVTCP${locale}_${year}.exe"
} | ConvertTo-Json
