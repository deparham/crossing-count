# Installing CrossingCount on Windows

## The warning you see

Windows shows **"Windows protected your PC"** (SmartScreen), or **"unknown publisher"**, or
the browser says the file is **not commonly downloaded** and may be dangerous.

This is not a virus warning and it does not mean anything is wrong with the file. Windows
says it because the installer is **not signed with a code-signing certificate**: it cannot
tell who made it. Every unsigned program gets the same message, however it was built.

## Installing it anyway (what to click)

1. Download `CrossingCount-Setup-<version>.exe` from the release page.
2. If the browser blocks the download: open its downloads list, find the file, choose
   **Keep** (Edge: the "..." menu > Keep; Chrome: Keep > Keep anyway).
3. Windows may mark a downloaded file as coming from the internet. To clear that, open
   PowerShell in the download folder and run:

   ```powershell
   Unblock-File .\CrossingCount-Setup-*.exe
   ```

   Or: right-click the file > **Properties** > tick **Unblock** at the bottom > OK.
4. Double-click the installer. At "Windows protected your PC", click **More info**, then
   **Run anyway**.
5. If Windows asks for an administrator ("Do you want to allow this app..."), the publisher
   line says *Unknown* until the build is signed. Installing for yourself only
   (the installer offers this) does not need an administrator.

## Checking you got the right file

Every release names the installer's SHA-256 checksum in its notes. Compare it with the file
you downloaded:

```powershell
Get-FileHash .\CrossingCount-Setup-0.1.42.exe -Algorithm SHA256
```

If the hash matches the one in the release notes, the file is exactly what the build
produced. If it does not, do not run it.

## Making the warning go away for good

The warning disappears only when the installer and the program are **signed**. The build
already signs them when a certificate is available: set two repository secrets and every
later build is signed, with no other change.

| Secret | What it is |
|---|---|
| `WINDOWS_CERT_PFX` | the certificate's `.pfx` file, base64 (PowerShell: `[Convert]::ToBase64String([IO.File]::ReadAllBytes("cert.pfx"))`) |
| `WINDOWS_CERT_PASSWORD` | its password |

Choosing a certificate:

- **Azure Trusted Signing** (Microsoft) — about US$10 a month, and issued to an individual or
  a business; the signing key stays with Microsoft. The cheapest route that clears
  SmartScreen. It needs a verified identity, and for an individual, three years of
  verifiable history.
- **OV code-signing certificate** (Sectigo, DigiCert and others) — roughly US$200-400 a
  year. Since June 2023 the key must live on a hardware token or an HSM, which a GitHub
  runner cannot use directly; signing then happens through the issuer's cloud service.
  SmartScreen still warns until the certificate builds up reputation over some downloads.
- **EV code-signing certificate** — around US$300-600 a year, hardware-bound, and trusted by
  SmartScreen immediately.

The certificate's name is what Windows will show as the publisher, so it should match the
name on the app (currently *iTOi Solutions*).

A self-signed certificate does **not** help: Windows does not trust it, and the warning
stays unless the certificate is installed on every computer as a trusted root, which is
worse than the warning.

## Updates

The app updates itself from inside its own window: it downloads the new installer and runs
it silently. That download does not come from a browser, so there is no "keep the file"
step, but an unsigned installer still shows the administrator prompt with an unknown
publisher. Signing removes that too.

## If the app will not start after installing

- It is installed in `C:\Program Files\Crossing Count` (or `%LOCALAPPDATA%\Programs` when
  installed for one user), and starts from the Start menu entry *Crossing Count*.
- Its log is `%LOCALAPPDATA%\CrossingCount\logs\app.log`.
- Some antivirus products quarantine new, unsigned programs. If the app disappears after
  installing, the antivirus's quarantine list will say so; signing avoids this too.
