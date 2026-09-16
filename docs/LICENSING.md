# Licensing

What CrossingCount is made of, what each part's licence allows, and the one thing that
would change everything. **This is not legal advice**: it is the engineering position, and
the person who owns the software should have a lawyer confirm it before anything here is
shared outside the company.

## 1. The thing to be careful about

> **CrossingCount uses Ultralytics YOLO, which is licensed AGPL-3.0, and its model weights
> are under the same licence.** As long as the software is only run inside the company that
> owns it, and only its *reports* go to customers, the AGPL's obligations are not
> triggered: nothing is conveyed to anyone.
>
> **Two things would trigger them:**
>
> 1. **Giving anyone outside the company a build** — a customer, a partner, a contractor on
>    their own account: the installer, the .app, the source, any of it.
> 2. **Letting anyone outside the company reach the app over a network** — hosting it,
>    putting it on a server, or binding it to anything other than this computer.
>
> Either one means the whole combined work must be offered under AGPL-3.0, source included,
> or Ultralytics' commercial licence must be bought first. There is no middle setting.

The app binds to `127.0.0.1` by default for exactly this reason, and refuses requests that
are not addressed to this computer (`localweb.py`). **Sharing on this network**
(`network.py`) lets other computers use it with an access code. Used by colleagues of the
same company on its own network, that is still use inside the company. Whether that is
enough under AGPL section 13, which speaks of *users interacting with it remotely*, is a
question for a lawyer before relying on it. Giving the code to anyone outside the company
(a customer, a contractor on their own account) is item 2 above.

**The way out**, if it is ever needed, is RF-DETR (Apache-2.0), already built in behind
`--detector rfdetr`: if it ever matches YOLO on the gold set, switching detector and
weights removes the AGPL question entirely. That is a better reason to keep testing it than
accuracy alone.

## 2. Colleagues' copies

Builds are published as private GitHub releases and colleagues install them. Within one
company, giving employees a copy of software the company owns is not usually "conveying" it
to a third party, so the AGPL position above holds. It stops holding if a person installing
it is not part of that company — a contractor working through their own business, a partner
firm, a customer's staff. If that is ever in question, ask the lawyer before sending the
link.

Those builds also carry **built-in RetailNext keys** (repository secrets baked in at build
time), which can be extracted from any copy. They are for colleagues only, never for
customers; if a laptop is lost, change the keys in RetailNext and build again.

## 3. Every dependency

Versions as locked on 16 September 2026 (`uv.lock`; `uv export` lists the full 307 with
their transitive dependencies).

| Component | Version | Licence | What it means for us |
|---|---|---|---|
| **ultralytics** (and the YOLO26/YOLO11 weights) | 8.4.149 | **AGPL-3.0** | The one restriction. Internal use is fine; conveying a build or exposing it over a network is not, without a commercial licence. |
| rfdetr | 1.10.1 | Apache-2.0 | Free to use and distribute, with attribution. Only the Large and smaller variants: XL and 2XL need `rfdetr[plus]`, which is licensed PML 1.0 and is **not** used. |
| torch | 2.14.0 | Apache-2.0 (with LLVM exception parts) | Free to use and distribute, with attribution. |
| torchvision | 0.29.0 | BSD-3-Clause | Free, attribution. |
| transformers | 5.17.0 | Apache-2.0 | Pulled in by rfdetr only. |
| supervision | 0.30.3 | MIT | Pulled in by rfdetr only. |
| opencv-python | 5.0.0.93 | Apache-2.0 | Free, attribution. |
| numpy | 2.4.6 | BSD-3-Clause and others | Free, attribution. |
| av (PyAV) | 18.1.0 | BSD-3-Clause | Bundles FFmpeg builds; the wheels used are LGPL-configured, so no GPL obligation arises from linking. |
| lap | 0.5.13 | BSD-2-Clause | Free, attribution. |
| fastapi | 0.141.1 | MIT | Free, attribution. |
| uvicorn | 0.52.4 | BSD-3-Clause | Free, attribution. |
| pywebview | 6.2.1 | BSD-3-Clause | The app's own window. |
| python-pptx | 1.0.2 | MIT | Writes the report. |
| reportlab | 5.0.1 | BSD-3-Clause | Writes the report as a PDF (standard fonts only, none bundled). |
| lxml | 6.1.3 | BSD-3-Clause | Through python-pptx. |
| pillow | 12.3.0 | MIT-CMU | Through python-pptx. |
| keyring | 25.7.0 | MIT | Keychain and Credential Manager. |
| tzdata | 2026.4 | Apache-2.0 | Time zones on Windows. |
| certifi | 2026.7.22 | **MPL-2.0** | Weak copyleft, file-level: using it unmodified imposes nothing. Do not modify its files. |
| httpx, pytest, mypy, ruff (development only) | — | BSD-3-Clause / MIT | Not shipped in the app. |
| pyinstaller (build only) | — | GPL-2.0 with an exception for the programs it bundles | The exception exists so bundled apps need not be GPL; it does not change anything above. |

Checked for known vulnerabilities with `pip-audit` on every change
(`.github/workflows/checks.yml`); on 16 September 2026 there were none.

## 4. CrossingCount itself

Proprietary: © 2026 Parham Forozan, all rights reserved. See [LICENSE](../LICENSE) and
`NOTICE`. Reports produced by the software are outputs, not copies of it, and can be given
to the customers whose footage was validated.

## 5. Other people's data

Footage, counts and reports belong to the retailer whose cameras produced them. Licensing
says nothing about that; [PRIVACY.md](PRIVACY.md) does.
