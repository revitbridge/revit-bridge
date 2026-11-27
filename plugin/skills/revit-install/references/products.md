# Products, Updates and Unit Schemas

## Official media and order

Read the media the user named from the download directory. Where an Autodesk custom deployment image already exists, use the deployment command and the manifest that come with the image. For `*_setup_webinstall.exe`, let the official launcher, from the verified publisher, fetch the product's signed metadata. A manifest API request put together by hand lacks the launcher's signature. To switch to a silent deployment, end the launcher's preparation stage first, confirm that no installation transaction has been committed, and then start the ODIS queue.

Use the Autodesk ODIS on the machine, with a valid signature, and the parameters that come with the media. The verified deployment layout is below. Keep the manifest's relative position and its companion files; a missing neighbouring Bundle directory makes ODIS fail with error 8.

```text
<STAGE>/
  Collection.xml
  <BUNDLE_ID>/
    setup.xml
    setup_ext.xml     (when the media provides it)
    ...               (the complete official Bundle directory)
```

When a Web Install is turned into a deployment, check the Collection's product, language, licence method and installation path. Revit's `CONTENT_PATH` is `<CONTENT_ROOT>\<YEAR>`, and deployment custom properties have to be passed on to the matching Bundle and Application. Use the product UPI2 from the official manifest; for a requested update, use that update's own UPI2. The generated Collection can be validated with `SetupRes/collection.xsd` of the ODIS on the machine. Keep the extension manifest and the download source that the official deployment script needs. The parameters correspond as follows; the exact command is the one of that release's media.

```text
Installer.exe -i deploy -q -o "<STAGE>\Collection.xml" --manifest "<STAGE>\<BUNDLE_ID>\setup.xml" --install_source "<STAGE>" REBOOT=ReallySuppress
```

When the installer has to write to machine-level directories or the 64-bit registry, run it as an administrator the user authorised. If access is still denied, find the exact key, file and process identity first, then handle it through the existing enterprise deployment mechanism. For a deployment under an enterprise service account, make sure that the script actually run, the task definition and the staged media are controlled by trusted administrators only; record the task identity and remove this run's task after it exits. Changing registry ACLs is not part of this procedure.

## Deciding whether the next item may start

| What you see | Next step |
| --- | --- |
| The log says finished, the installer is still running | Wait for that native process to end. ODIS may still be writing logs and registration data. |
| Exit code 0 | Check the target version, the required components and the content path on the machine, then commit the success record. |
| Exit code 3010 | Accept what was installed and record the pending restart. Continue when the following media allow the restart to be deferred. |
| MSI 1618, or an installation transaction already exists | Stop dispatching and wait for the existing transaction to end. Check whether a rollback happened and resume by the components still missing. |
| Any other failure, or a rollback | Save the native exit code, the failed component, and the ODIS logs and MSI log under the account that actually ran. Find and fix the cause before resuming the queue. |

Under the installing account, read the end of ODIS's `Install.log`, `DDA.log` and `ProcessManager.log`, and the MSI log of the failed component. While a log is open its modification time may lag. To defer a restart, use `REBOOT=ReallySuppress` where the media supports it and `/norestart` for an MSI. Do not close the user's software to move the queue on.

## Accepting a version

Check the files below in the product's actual installation directory, and cross-check them against the build the official package is expected to install and against the updates Windows lists as installed. A product's marketing version and its binary version can differ, and an AutoCAD file version can carry a letter prefix. Civil 3D may already bring the AutoCAD update of the same year.

| Product | Version evidence |
| --- | --- |
| Revit | `Revit <YEAR>\Revit.exe`, plus the Unit Schemas and the RCL content path |
| Navisworks Manage | `Navisworks Manage <YEAR>\Roamer.exe` |
| Civil 3D | `AutoCAD <YEAR>\C3D\AeccUiLand.arx` and the Civil 3D Update registration entry |
| AutoCAD | `AutoCAD <YEAR>\acad.exe` and the AutoCAD Update registration entry |

When the exit code is 0 and an acceptance path is missing, first find out which file name that release really uses. If the acceptance rule was wrong, keep the original record and attach the corrected evidence, so that nothing is installed a second time.

## Revit reports "Missing schema folder"

1. Look at `%CommonProgramFiles%\Autodesk Shared\Revit Schemas <YEAR>` and at the build of Revit actually installed. The Unit Schemas are a shared program component; they are accepted separately from the content libraries on the configurable drive.
2. Find `RevitUnitSchemas.msi` in the official media of the matching build or in the original cache. Check the Autodesk signature, the hash, and the MSI's product name, ProductCode and ProductVersion against the installed build. Different patches of one year can use different versions of the component.
3. After every other installation has ended, repair the component with administrator rights through the official maintenance route. If a rollback removed the component, the matching MSI can be installed with `msiexec /i "<MSI>" /qn /norestart REBOOT=ReallySuppress /L*v "<LOG>"`. Record the native exit code, then check that the schema directory holds real data and no zero-byte file.
4. Report the build that is actually there after the repair, and record it apart from the update that was planned. The repair only brings that year's Revit back to some working build; when that build is lower than the planned update, the planned update is not a success because of it.
