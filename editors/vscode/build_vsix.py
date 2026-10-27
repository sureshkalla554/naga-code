#!/usr/bin/env python3
"""Package this folder as a .vsix (a zip with a manifest), without needing npm or vsce.

    python3 editors/vscode/build_vsix.py            # -> editors/vscode/dist/naga-<version>.vsix
    code --install-extension editors/vscode/dist/naga-<version>.vsix
"""
import json
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

HERE = Path(__file__).resolve().parent
FILES = ["package.json", "extension.js", "README.md", "LICENSE"]

pkg = json.loads((HERE / "package.json").read_text())
out = HERE / "dist" / f"{pkg['name']}-{pkg['version']}.vsix"
out.parent.mkdir(exist_ok=True)

manifest = f"""<?xml version="1.0" encoding="utf-8"?>
<PackageManifest Version="2.0.0" xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011">
  <Metadata>
    <Identity Language="en-US" Id="{pkg['name']}" Version="{pkg['version']}" Publisher="{pkg['publisher']}"/>
    <DisplayName>{escape(pkg['displayName'])}</DisplayName>
    <Description xml:space="preserve">{escape(pkg['description'])}</Description>
    <Categories>{','.join(pkg.get('categories', []))}</Categories>
    <Properties>
      <Property Id="Microsoft.VisualStudio.Code.Engine" Value="{pkg['engines']['vscode']}"/>
    </Properties>
  </Metadata>
  <Installation><InstallationTarget Id="Microsoft.VisualStudio.Code"/></Installation>
  <Dependencies/>
  <Assets>
    <Asset Type="Microsoft.VisualStudio.Code.Manifest" Path="extension/package.json" Addressable="true"/>
  </Assets>
</PackageManifest>
"""
content_types = """<?xml version="1.0" encoding="utf-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension=".json" ContentType="application/json"/>
  <Default Extension=".js" ContentType="application/javascript"/>
  <Default Extension=".md" ContentType="text/markdown"/>
  <Default Extension=".vsixmanifest" ContentType="text/xml"/>
  <Default Extension="" ContentType="text/plain"/>
</Types>
"""
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    z.writestr("extension.vsixmanifest", manifest)
    z.writestr("[Content_Types].xml", content_types)
    for name in FILES:
        src = HERE / name if (HERE / name).exists() else HERE.parents[1] / name
        if src.exists():
            z.write(src, f"extension/{name}")
print(out)
