"""Install a user-local Finder/Dock launcher for an already installed kaki-app command."""

from __future__ import annotations

import argparse
import plistlib
import shlex
import shutil
import sys
from pathlib import Path

from PIL import Image, ImageDraw

from kaki import __version__


def install(destination: Path, executable: Path):
    # 生成用户级 Finder/Dock 启动器；只更新标识属于 Kaki 的 App，不替换同名第三方应用。
    if sys.platform != "darwin":
        raise SystemExit("This launcher is for macOS")
    if not executable.is_file():
        raise SystemExit("Install kaki-agent with uv tool install first")
    if destination.exists():
        plist = destination / "Contents/Info.plist"
        try:
            ours = plistlib.loads(plist.read_bytes()).get("CFBundleIdentifier") == "io.kaki.agent"
        except (OSError, ValueError):
            ours = False
        if not ours:
            raise SystemExit("Refusing to replace an unrelated App")
    macos = destination / "Contents/MacOS"
    resources = destination / "Contents/Resources"
    macos.mkdir(parents=True, exist_ok=True)
    resources.mkdir(parents=True, exist_ok=True)
    launch = macos / "Kaki"
    # Finder does not inherit an interactive shell PATH. Keep user-installed Go and developer tools available.
    text = (
        '#!/bin/bash\nexport PATH="$HOME/.local/bin:$HOME/go/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"\nexec '
        + shlex.quote(str(executable))
        + ' "$@"\n'
    )
    launch.write_text(text)
    launch.chmod(0o755)
    icon = Image.new("RGBA", (1024, 1024))
    draw = ImageDraw.Draw(icon)
    draw.rounded_rectangle((24, 24, 1000, 1000), radius=280, fill="#E77348")
    draw.line([(337, 268), (337, 758)], fill="white", width=106)
    draw.line([(688, 278), (380, 520), (705, 754)], fill="white", width=106, joint="curve")
    for x, y in [(337, 268), (337, 758), (688, 278), (705, 754)]:
        draw.ellipse((x - 53, y - 53, x + 53, y + 53), fill="white")
    icon.save(resources / "Kaki.icns", format="ICNS")
    data = {
        "CFBundleName": "Kaki",
        "CFBundleDisplayName": "Kaki",
        "CFBundleIdentifier": "io.kaki.agent",
        "CFBundleExecutable": "Kaki",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": __version__,
        "CFBundleVersion": __version__,
        "CFBundleIconFile": "Kaki.icns",
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "12.0",
    }
    (destination / "Contents/Info.plist").write_bytes(plistlib.dumps(data))
    print(destination)


def main():
    # 安装器仅负责命令/路径选择，真正运行仍调用已经安装的 kaki-app。
    parser = argparse.ArgumentParser()
    parser.add_argument("--destination", type=Path, default=Path.home() / "Applications/Kaki.app")
    parser.add_argument(
        "--executable",
        type=Path,
        default=Path(shutil.which("kaki-app") or Path.home() / ".local/bin/kaki-app"),
    )
    args = parser.parse_args()
    install(args.destination.expanduser().resolve(), args.executable.expanduser().resolve())


if __name__ == "__main__":
    main()
