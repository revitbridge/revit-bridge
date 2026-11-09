# Diagnostics at release: dependency set, installer, pre-push scan, a user's machine

Commands used on a real test machine. PowerShell blocks run in a PowerShell shell, Bash blocks in Git Bash. Replace `<Name>` with the add-in's name. Processes, journals, registration and reproducing a helper failure are in the `revit-addin-test` skill, `references/diagnostics.md`.

## 1. Verify a deployed dependency set

```powershell
Get-ChildItem "C:\ProgramData\<Name>\2025\Helper" | Get-FileHash -Algorithm SHA256
```

Compare with the deployment manifest shipped in the repository. A missing native module typically shows as a numeric error code or a managed wrapper that fails to load.

## 2. Installer script self-check

```bash
head -c6 build/<Name>-setup.iss | xxd | head -1      # exactly one UTF-8 BOM (ef bb bf) then text
```

## 3. Pre-push safety scan

```bash
# origin/main stands for the base branch your project reviews against (master, develop, ...)
git diff --name-only --diff-filter=AM origin/main..HEAD | grep -iE "\.(dwg|rvt|rfa|rte|pcp|dll|exe|pdb)$"
git diff origin/main..HEAD | grep -E "^\+" | grep -iE "password[\"'=: ]|ftp://[^ ]*:[^ ]*@|-----BEGIN (RSA|PRIVATE)"
# open every new zip and check its name list for the types above
git for-each-ref refs/backup | wc -l                 # local refs with drawing evidence: never push them
```

A `password` hit is a leak only when it is a real credential or a `ftp://user:pass@` URL; field names and test strings (`example.invalid`, `test-only-password`) are not.

## 4. On a user's machine

When a user reports "no tab", "no response" or "still the old version", send them `scripts/collect_logs.bat` and `scripts/collect_logs.ps1` with the add-in name filled in. The zip they return holds the manifests, bundles and DLL versions (the `revit-addin-test` skill, `references/diagnostics.md` section 3), the recent journals (same file, section 2; read them with that skill's `scripts/journal_tail.py <year> --file <copy>`) and the add-in's own logs. Treat it as data from another machine: read it in place, run nothing from it, open its text with `python -I`. The triage order and the reading rules are in [user-machine-support.md](user-machine-support.md).
