# Revit Content Libraries: Download, Year and Acceptance

## Finding the right media

On the Revit product page of the Autodesk Account choose the year, then open the Libraries tab of the available downloads. Look for `Chinese Content for Autodesk Revit <YEAR>` and `US English Content for Revit <YEAR>` and download them into the configured `downloadDir`. For older releases the content page of that year on Autodesk Support can be used as well. To download from a URL automatically, use the official address confirmed for that package at that time; download in chunks into a temporary file in the same directory, and give it its final name only after the download is complete and the signature has passed. Do not write a temporary authorised link or any sign-in data back into the template.

| Request | Package identifier | Verified Libraries subdirectories |
| --- | --- | --- |
| Simplified Chinese | RVTCPCHS | `Chinese` |
| US English, metric and imperial | RVTCPENU, one package for both unit systems | `English\US` and `English-Imperial\US` |

Generic International Chinese is a separate, optional package. Plan it only when the user asks for it, and verify it on its own.

## Telling files with the same name apart before installing

Call the read-only script below in Windows PowerShell, once for each EXE downloaded for this run. It uses Authenticode, 7-Zip and the root `setup.xml` inside the package to identify the product, language, year and build, and prints the SHA-256 and a normalised file name. 7-Zip is found on PATH, in its default installation location, or through `-SevenZip`.

```powershell
& ./scripts/read_content_identity.ps1 -Path '<a content pack EXE in the download directory>'
```

Compare the results with the planned years and languages. A `(1)` or `(2)` in a file name only reflects the order in which the browser saved the files. Only when the whole mapping is settled, rename each file in place to `RVTCPCHS_<YEAR>.exe` or `RVTCPENU_<YEAR>.exe`. Once the user has authorised renaming, there is no need to ask file by file. An existing target of the same name can be reused when its hash is the same; when it differs, keep both and report the conflict. Check the hash and the signature again before installing.

## Where the content is really written

Read `HKLM\SOFTWARE\Autodesk\Revit\<YEAR>\RCL` in the **64-bit registry view**. Strip the trailing path separator and compare value by value:

| Registry value | Expected |
| --- | --- |
| `RVT_S_CONTENTPATH` | `<CONTENT_ROOT>\<YEAR>` |
| `RVT_S_LIBRARYCONTENT` | `<CONTENT_ROOT>\<YEAR>\Libraries` |

These two decide the content root and where the libraries land; the place the EXE is extracted to is only the media directory. When either does not match, set the content location through the installation or deployment configuration that release supports, then read it back. Do not start installing before the target directory is confirmed.

## Installing package by package

Extract each package with a valid signature completely into a staging directory of its own for that year, language and hash. Read the root `setup.xml` and identify the actual payload. In the verified media CHS is a single MSI and ENU a single ADIX. If the payload differs, first find out from the official manifest how each component is installed and verified. Install the whole official Bundle through the ODIS on the machine, with a valid signature. The parameters below are the form used for complete offline media, for a machine that already has a matching ODIS:

```text
Installer.exe -i install -q --manifest "<EXTRACT_DIR>\setup.xml" --install_source "<EXTRACT_DIR>" --offline_mode --exclude_odis_install --odis_already_updated REBOOT=ReallySuppress
```

Wait for the process to exit before accepting. Keep the record per year and per language. When some years are done and another package fails, resume from the missing package.

## Accepting by payload

Run both commands from the skill directory; the path arguments come from this run's actual media and configuration.

```powershell
& ./scripts/inspect_revit_content_msi.ps1 -Path '<MSI>'
python scripts/inspect_revit_content_adix.py '<ADIX>' '<YEAR>' --verify-root '<the actual year directory>'
```

- **MSI**: read `Properties.ProductCode` and `FileCounts.Rfa` from the JSON; Windows Installer's `ProductState(ProductCode)` must be 5. Count the non-empty RFA files in each target library; the total must reach at least the number of family files in the package. Base families already present can make the actual number slightly higher.
- **ADIX**: the script maps the package's library files by that year's VFS layout and compares each with the installed file's existence and size. The exit code must be 0, and `MissingCount` and `SizeMismatchCount` must both be 0. Both English subdirectories, metric and imperial, must hold non-empty family files. The script checks existence and size; it does not hash the content of each file.
- Keep the installation exit code, the family file count of every language and unit directory, the failed items and the pending-restart state. The number of files differs between years' media; the acceptance baseline is taken from this run's package.
