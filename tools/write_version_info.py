"""Write the Windows version resource for DOT.exe (PyInstaller --version-file).

Shows in DOT.exe > Properties > Details: product name, description, and version.
Usage: python tools/write_version_info.py build/version_info.txt
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dotdm import __version__  # noqa: E402


def main(target: str):
    numbers = tuple(([int(part) for part in __version__.split(".")] + [0, 0, 0, 0])[:4])
    text = f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={numbers}, prodvers={numbers}, mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'DOT'),
      StringStruct('FileDescription', 'DOT Download Manager'),
      StringStruct('FileVersion', '{__version__}'),
      StringStruct('InternalName', 'DOT'),
      StringStruct('LegalCopyright', 'DOT Download Manager'),
      StringStruct('OriginalFilename', 'DOT.exe'),
      StringStruct('ProductName', 'DOT Download Manager'),
      StringStruct('ProductVersion', '{__version__}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    print(f"Version resource for DOT {__version__} written to {path}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else str(ROOT / "build" / "version_info.txt"))
