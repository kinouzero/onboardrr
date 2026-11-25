"""Build a standalone executable on the target operating system."""

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

root = Path(SPECPATH).parent
analysis = Analysis(
    [str(root / "distribution" / "entrypoint.py")],
    pathex=[str(root)],
    datas=[
        (str(root / "onboardrr" / "tui.tcss"), "onboardrr"),
        (str(root / "onboardrr" / "freebox-ca.pem"), "onboardrr"),
        *((str(script), "onboardrr/remote")
          for script in (root / "onboardrr" / "remote").iterdir()
          if script.suffix in {".py", ".sh"}),
        (str(root / "provisioning"), "onboardrr/provisioning"),
        *collect_data_files("textual"),
        *copy_metadata("textual"),
    ],
    hiddenimports=[
        *collect_submodules("rich._unicode_data"),
        *collect_submodules("textual.widgets"),
        *collect_submodules("textual.drivers"),
    ],
)
archive = PYZ(analysis.pure)
executable = EXE(
    archive, analysis.scripts, analysis.binaries, analysis.datas,
    name="onboardrr",
    console=True,
)
