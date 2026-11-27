---
name: revit-install
description: Install Autodesk Revit and the desktop products that go with it on Windows from a queue - Revit, Navisworks Manage, Civil 3D, AutoCAD - and identify, rename and install the Revit Chinese and US English content libraries (families) year by year. Use for local media, the official Web Install, resuming an interrupted installation, and repairing the Revit Unit Schemas ("Missing schema folder"). 在 Windows 上按队列安装 Autodesk Revit、Navisworks Manage、Civil 3D、AutoCAD，或识别、重命名并按年份安装 Revit 中英文族库；适用于本地介质、官方 Web Install、安装中断续跑和 Unit Schemas 修复。Needs no revit-bridge component.
---

# Installing Autodesk Products and Revit Content Libraries

Answer in the user's language.

## Settle the scope of this run first

Copy the [configuration template](assets/install-config.example.json) into this run's working directory and leave the template in the skill as it is. Fill `downloadDir` and `contentRoot` with the absolute paths the user gave. The first is both where new downloads are saved and where existing installers are looked for. The second is the parent directory only; the skill appends the year once. Windows paths may be written with forward slashes in JSON. Set the proxy as this run requires, or JSON `null` to keep the existing connection.

The seven products and four content years in the template are examples: change them to what this run installs and delete everything outside its scope. For content libraries only, set `products` to `[]`. For products only, set `contentYears` to `[]`; if no Revit product is listed either, `contentRoot` may be `null`. Run the command below with Python 3. It only prints the plan, and it exits when a required placeholder has not been replaced. Handle only the items in this plan, even when the target drive already holds older packages.

```powershell
python scripts/plan.py '<configuration file of this run>'
```

## Run and resume

1. Take stock of the media already in the configured download directory and check the Autodesk digital signature of each file. For a missing package, choose the product, year and language in the currently authorised Autodesk Account and download it into the same directory. Signing in and approving UAC are the user's actions. Save the verified file name, size, SHA-256 and version. Check the hash again before a later run, and keep one extraction directory per product and year.
2. For the products, read [product deployment and troubleshooting](references/products.md). Run one installer at a time, in the order of the plan. For content libraries only, first confirm that Revit of the target year is installed and that its content location is right, then go straight to the content procedure.
3. For the Chinese, English metric and English imperial libraries, read [identify, install and accept](references/content.md). Take the year and the language from the metadata inside the package, check the whole batch of target file names, and only then rename. Install each year's content into the year directory the plan gives.
4. For every item record the media hash, the target, the actual version or the content file verification, the native exit code and whether a restart is pending. Save "installing" and the process identity first, then wait for the native exit; this keeps two executors from starting the same queue. When resuming, check the machine's state again and skip identical items that already passed acceptance. After a request to pause, stop dispatching the next item and leave the current transaction as it is. When an item fails, keep the original logs, find the cause, and then decide where to retry from.

## Proxy and desktop

Where the machine or its organisation already has a rule for proxies, follow that rule first. The process proxy, the interactive user's WinINET settings and a service account's WinINET settings may each take effect on their own. Change the settings of the account that actually installs only for the time of this run, only when this run needs it, and only with the matching authorisation. Persist the original values first, including which keys existed; restore them exactly on success, failure or cancellation, and read them back. Before restoring, check that the current values are still the ones this run set; if the user changed them in the meantime, ask the user to decide. A proxy port or an authorisation from an earlier run is never reused with the skill.

Defer restarts by default, and stop at the item that depends on a forced restart. Leave the user's Revit sessions and desktop input alone. Accept by installed files, registration data and logs; when a family has to be loaded in Revit to prove something, hand that to the user.
