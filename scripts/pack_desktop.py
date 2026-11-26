#!/usr/bin/env python3
"""Build desktop installers and portable bundles for the current OS."""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import zipfile
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_NAME = 'ReclaimMyChats'
PRODUCT_NAME = 'Reclaim My Chats'
BUNDLE_ID = 'org.reclaimmychats.app'
ARCHIVE_NAME = 'Reclaim-My-Chats'
EXCLUDED_MODULES = (
    'pytest', 'numpy', 'PIL', 'scipy', 'skimage',
    'flet_cli', 'PyInstaller', 'setuptools', 'pip',
)

_APPRUN_SCRIPT = """#!/bin/sh
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
export XDG_DATA_DIRS="$HERE/usr/share:${XDG_DATA_DIRS:-/usr/local/share:/usr/share}"
exec "$HERE/usr/lib/ReclaimMyChats/ReclaimMyChats" "$@"
"""

_APPIMAGE_STUB = """#!/bin/sh
set -eu
SELF="$0"
case "$SELF" in
  */*) ;;
  *) SELF="$(command -v "$SELF" || printf '%s' "$SELF")" ;;
esac
if [ "${1:-}" = "--appimage-extract" ]; then
  mkdir -p squashfs-root
  tail -n +__START_LINE__ "$SELF" | tar -xzf - -C squashfs-root
  exit 0
fi
CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/reclaimmychats/appimage-__VERSION__-__DIGEST__"
STAMP="$CACHE/.extracted"
if [ ! -f "$STAMP" ]; then
  TMP="${CACHE}.tmp.$$"
  rm -rf "$TMP"
  mkdir -p "$TMP"
  tail -n +__START_LINE__ "$SELF" | tar -xzf - -C "$TMP"
  : > "$TMP/.extracted"
  rm -rf "$CACHE"
  mv "$TMP" "$CACHE"
fi
exec "$CACHE/AppRun" "$@"
"""


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(kind + data) & 0xFFFFFFFF
    return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', crc)


def _icon_pixel(nx: float, ny: float) -> bytes:
    if nx**4 + ny**4 > 0.88**4:
        return b'\x00\x00\x00\x00'
    bubble = (nx / 0.50) ** 2 + ((ny + 0.06) / 0.36) ** 2 <= 1.0
    tail = -0.26 <= nx <= -0.04 and 0.20 <= ny <= 0.44 and nx + ny <= 0.20
    if bubble or tail:
        return b'\xff\xff\xff\xff'
    return b'\x25\x63\xeb\xff'


def _icon_png_bytes(size: int = 256) -> bytes:
    """Render a minimal 256x256 RGBA PNG application icon without Pillow."""
    half = size / 2
    rows = bytearray()
    for y in range(size):
        rows.append(0)
        ny = (y + 0.5 - half) / half
        for x in range(size):
            rows.extend(_icon_pixel((x + 0.5 - half) / half, ny))
    ihdr = struct.pack('>IIBBBBB', size, size, 8, 6, 0, 0, 0)
    idat = zlib.compress(bytes(rows), 9)
    return (
        b'\x89PNG\r\n\x1a\n'
        + _png_chunk(b'IHDR', ihdr)
        + _png_chunk(b'IDAT', idat)
        + _png_chunk(b'IEND', b'')
    )


def _icon_ico_bytes(size: int = 32) -> bytes:
    """Render an uncompressed 32-bit BGRA BMP icon wrapped in an `.ico` container."""
    half = size / 2
    xor_rows = bytearray()
    for y in range(size - 1, -1, -1):  # BMP rows are bottom-to-top
        ny = (y + 0.5 - half) / half
        for x in range(size):
            r, g, b, a = _icon_pixel((x + 0.5 - half) / half, ny)
            xor_rows.extend((b, g, r, a))
    and_mask = b'\x00' * (size * ((size + 31) // 32) * 4)
    dib_header = struct.pack('<IiiHHIIiiII', 40, size, size * 2, 1, 32, 0,
                             len(xor_rows), 0, 0, 0, 0)
    dib = dib_header + bytes(xor_rows) + and_mask
    ico_header = struct.pack('<HHH', 0, 1, 1)
    ico_entry = struct.pack('<BBBBHHII', size, size, 0, 0, 1, 32, len(dib), 22)
    return ico_header + ico_entry + dib


def write_icons(stage: Path) -> dict[str, Path]:
    """Write `.png`, `.ico`, and `.icns` icon files derived from one RGBA image."""
    icon_dir = stage / 'icons'
    icon_dir.mkdir(parents=True, exist_ok=True)
    png_bytes = _icon_png_bytes(256)
    png_path = icon_dir / f'{BUNDLE_ID}.png'
    png_path.write_bytes(png_bytes)

    ico_path = icon_dir / f'{BUNDLE_ID}.ico'
    ico_path.write_bytes(_icon_ico_bytes(32))

    icns_path = icon_dir / f'{BUNDLE_ID}.icns'
    ic08 = b'ic08' + struct.pack('>I', 8 + len(png_bytes)) + png_bytes
    icns_path.write_bytes(b'icns' + struct.pack('>I', 8 + len(ic08)) + ic08)
    return {'png': png_path, 'ico': ico_path, 'icns': icns_path}


def _rewrite_desktop_entry(src: Path, dst: Path, exec_cmd: str) -> None:
    lines = src.read_text(encoding='utf-8').splitlines()
    out: list[str] = []
    for line in lines:
        if line.startswith('Exec='):
            out.append(f'Exec={exec_cmd}')
        elif line.startswith('Icon='):
            out.append(f'Icon={BUNDLE_ID}')
        else:
            out.append(line)
    if not any(line.startswith('StartupWMClass=') for line in out):
        out.append(f'StartupWMClass={BUNDLE_ID}')
    dst.write_text('\n'.join(out) + '\n', encoding='utf-8')


def build_linux_tarball(app_dir: Path, desktop: Path, icon: Path, archive: Path) -> Path:
    """Create the relocatable `.tar.gz` bundle from the unpacked PyInstaller folder."""
    archive.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        staged_desktop = Path(tmp) / desktop.name
        _rewrite_desktop_entry(desktop, staged_desktop, APP_NAME)
        with tarfile.open(archive, 'w:gz') as bundle:
            bundle.add(app_dir, arcname=APP_NAME)
            bundle.add(staged_desktop, arcname=f'{APP_NAME}/{desktop.name}')
            bundle.add(icon, arcname=f'{APP_NAME}/{icon.name}')
    return archive


def build_linux_appimage(
    app_dir: Path,
    desktop: Path,
    icon: Path,
    version: str,
    output_path: Path,
) -> Path:
    """Build a self-extracting AppImage containing an unpacked AppDir payload."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        appdir = tmp_path / 'AppDir'
        appdir.mkdir()
        apprun = appdir / 'AppRun'
        apprun.write_text(_APPRUN_SCRIPT, encoding='utf-8')
        apprun.chmod(0o755)
        _rewrite_desktop_entry(desktop, appdir / desktop.name, 'AppRun')
        shutil.copy2(icon, appdir / f'{BUNDLE_ID}.png')
        shutil.copytree(app_dir, appdir / 'usr' / 'lib' / APP_NAME, symlinks=True)

        payload_path = tmp_path / 'payload.tar.gz'
        with tarfile.open(payload_path, 'w:gz') as tar:
            for item in sorted(appdir.iterdir()):
                tar.add(item, arcname=item.name)
        payload = payload_path.read_bytes()

    digest = hashlib.sha256(payload).hexdigest()[:12]
    start_line = len(_APPIMAGE_STUB.splitlines()) + 1
    stub = (
        _APPIMAGE_STUB.replace('__START_LINE__', str(start_line))
        .replace('__VERSION__', version)
        .replace('__DIGEST__', digest)
    )
    output_path.write_bytes(stub.encode('utf-8') + payload)
    output_path.chmod(0o755)
    return output_path


def _extract_zip_safely(archive: Path, dest: Path) -> None:
    dest_root = dest.resolve()
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            target = (dest_root / info.filename).resolve()
            if target != dest_root and not target.is_relative_to(dest_root):
                raise RuntimeError(f'unsafe zip entry: {info.filename}')
        zf.extractall(dest)


def _extract_client_archive(app_base: Path, system: str) -> Path:
    if system == 'windows':
        archive = app_base / 'flet-windows.zip'
        if not archive.is_file():
            raise RuntimeError(f'bundled Flet client archive not found: {archive}')
        _extract_zip_safely(archive, app_base)
        return archive
    matches = sorted(app_base.glob('flet-linux-*.tar.gz'))
    if not matches:
        raise RuntimeError(f'bundled Flet client archive not found under {app_base}')
    archive = matches[0]
    with tarfile.open(archive, 'r:gz') as tf:
        tf.extractall(app_base, filter='data')
    return archive


def unpack_bundled_flet_client(app_dir: Path, system: str) -> Path:
    """Unpack the Flet client inside `--onedir` bundles so first launch skips `~/.flet`."""
    app_base = app_dir / '_internal' / 'flet_desktop' / 'app'
    exe_name = 'flet.exe' if system == 'windows' else 'flet'
    client_dir = app_base / 'flet'
    if (client_dir / exe_name).is_file():
        return client_dir
    archive = _extract_client_archive(app_base, system)
    if not (client_dir / exe_name).is_file():
        raise RuntimeError(f'unpacked Flet client missing {client_dir / exe_name}')
    archive.unlink(missing_ok=True)
    archive.with_name(archive.name + '.sha256').unlink(missing_ok=True)
    (app_base / '.last-used').unlink(missing_ok=True)
    return client_dir


def _ar_member(name: str, data: bytes) -> bytes:
    header = (
        f'{name:<16}'
        f'0           '
        f'0     '
        f'0     '
        f'100644  '
        f'{len(data):<10}'
        '`\n'
    ).encode('ascii')
    pad = b'\n' if len(data) % 2 else b''
    return header + data + pad


def _write_deb_ar(
    pkg_root: Path,
    output_path: Path,
    symlinks: dict[str, str] | None = None,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        control_tar = tmp_path / 'control.tar.gz'
        data_tar = tmp_path / 'data.tar.gz'
        with tarfile.open(control_tar, 'w:gz') as tar:
            for item in sorted((pkg_root / 'DEBIAN').iterdir()):
                tar.add(item, arcname=f'./{item.name}')
        with tarfile.open(data_tar, 'w:gz') as tar:
            for item in sorted(pkg_root.iterdir()):
                if item.name != 'DEBIAN':
                    tar.add(item, arcname=f'./{item.name}')
            for arcname, target in (symlinks or {}).items():
                info = tarfile.TarInfo(arcname)
                info.type = tarfile.SYMTYPE
                info.linkname = target
                info.mode = 0o777
                tar.addfile(info)
        output_path.write_bytes(
            b'!<arch>\n'
            + _ar_member('debian-binary', b'2.0\n')
            + _ar_member('control.tar.gz', control_tar.read_bytes())
            + _ar_member('data.tar.gz', data_tar.read_bytes())
        )


def build_linux_deb(
    app_dir: Path,
    desktop: Path,
    icon: Path,
    version: str,
    arch: str,
    output_path: Path,
) -> Path:
    """Create a `.deb` package installing under `/opt/ReclaimMyChats`."""
    deb_arch = {'x86_64': 'amd64', 'arm64': 'arm64'}.get(arch, arch)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        pkg_root = Path(tmp) / 'deb'
        debian_dir = pkg_root / 'DEBIAN'
        debian_dir.mkdir(parents=True)
        control = debian_dir / 'control'
        control.write_text(
            f'Package: reclaim-my-chats\n'
            f'Version: {version}\n'
            f'Section: utils\n'
            f'Priority: optional\n'
            f'Architecture: {deb_arch}\n'
            f'Maintainer: Reclaim My Chats <noreply@reclaimmychats.invalid>\n'
            f'Depends: libgtk-3-0, libglib2.0-0\n'
            f'Description: Save AI chat history to a local Markdown database\n'
            f' Desktop application for saving conversations from ChatGPT,\n'
            f' Claude, Google Gemini, Google AI Studio, DeepSeek, and Kimi.\n',
            encoding='utf-8',
        )
        control.chmod(0o644)
        debian_dir.chmod(0o755)
        shutil.copytree(app_dir, pkg_root / 'opt' / APP_NAME, symlinks=True)
        bin_dir = pkg_root / 'usr' / 'bin'
        bin_dir.mkdir(parents=True)
        apps_dir = pkg_root / 'usr' / 'share' / 'applications'
        apps_dir.mkdir(parents=True)
        _rewrite_desktop_entry(desktop, apps_dir / desktop.name, f'/opt/{APP_NAME}/{APP_NAME}')
        icons_dir = pkg_root / 'usr' / 'share' / 'icons' / 'hicolor' / '256x256' / 'apps'
        icons_dir.mkdir(parents=True)
        shutil.copy2(icon, icons_dir / f'{BUNDLE_ID}.png')

        dpkg_deb = shutil.which('dpkg-deb')
        if dpkg_deb:
            (bin_dir / 'reclaim-my-chats').symlink_to(f'/opt/{APP_NAME}/{APP_NAME}')
            _chmod_deb_tree(pkg_root)
            subprocess.run([dpkg_deb, '--build', '--root-owner-group',
                            str(pkg_root), str(output_path)], check=True)
        else:
            _chmod_deb_tree(pkg_root)
            _write_deb_ar(
                pkg_root,
                output_path,
                symlinks={'./usr/bin/reclaim-my-chats': f'/opt/{APP_NAME}/{APP_NAME}'},
            )
    return output_path


def _chmod_deb_tree(pkg_root: Path) -> None:
    pkg_root.chmod(0o755)
    for path in pkg_root.rglob('*'):
        if path.is_symlink():
            continue
        if path.is_dir():
            path.chmod(0o755)
        elif path.is_file():
            mode = path.stat().st_mode & 0o777
            path.chmod(mode | 0o444 | (0o111 if mode & 0o100 else 0))


def build_windows_zip(app_dir: Path, archive: Path) -> Path:
    """Zip the unpacked Windows `--onedir` folder into a portable bundle."""
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(app_dir.rglob('*')):
            if path.is_file():
                rel = path.relative_to(app_dir).as_posix()
                bundle.write(path, arcname=f'{APP_NAME}/{rel}')
    return archive


def write_inno_script(
    app_dir: Path,
    icon_ico: Path,
    version: str,
    output_exe: Path,
    iss_path: Path,
) -> Path:
    """Write an Inno Setup `.iss` script for a per-user, non-admin installer."""
    output_exe.parent.mkdir(parents=True, exist_ok=True)
    iss_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        '[Setup]',
        'AppId={{9E8A3D4C-4E12-4F5B-9A2E-7C1D6F3A2B10}',
        f'AppName={PRODUCT_NAME}',
        f'AppVersion={version}',
        f'DefaultDirName={{localappdata}}\\Programs\\{PRODUCT_NAME}',
        f'DefaultGroupName={PRODUCT_NAME}',
        'PrivilegesRequired=lowest',
        f'OutputDir={output_exe.parent}',
        f'OutputBaseFilename={output_exe.stem}',
        f'SetupIconFile={icon_ico}',
        f'UninstallDisplayIcon={{app}}\\{APP_NAME}.exe',
        'Compression=lzma2',
        'SolidCompression=yes',
        'WizardStyle=modern',
        '',
        '[Tasks]',
        ('Name: "autostart"; Description: "Start Reclaim My Chats when I sign in to Windows"; '
         'Flags: unchecked'),
        '',
        '[Files]',
        (f'Source: "{app_dir}\\*"; DestDir: "{{app}}"; '
         'Flags: ignoreversion recursesubdirs createallsubdirs'),
        '',
        '[Icons]',
        f'Name: "{{group}}\\{PRODUCT_NAME}"; Filename: "{{app}}\\{APP_NAME}.exe"',
        f'Name: "{{group}}\\Uninstall {PRODUCT_NAME}"; Filename: "{{uninstallexe}}"',
        '',
        '[Registry]',
        (r'Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; '
         r'ValueType: string; ValueName: "ReclaimMyChats"; '
         r'ValueData: """{app}\ReclaimMyChats.exe"""; Flags: uninsdeletevalue; Tasks: autostart'),
        '',
        '[Run]',
        (f'Filename: "{{app}}\\{APP_NAME}.exe"; '
         f'Description: "Launch {PRODUCT_NAME}"; '
         'Flags: nowait postinstall skipifsilent runasoriginaluser'),
        '',
    ]
    iss_path.write_text('\n'.join(lines), encoding='utf-8')
    return iss_path


def find_inno_compiler() -> str | None:
    """Locate Inno Setup compiler `iscc` on PATH or the default Windows install path."""
    found = shutil.which('iscc') or shutil.which('ISCC.exe')
    if found:
        return found
    default = Path(r'C:\Program Files (x86)\Inno Setup 6\ISCC.exe')
    return str(default) if default.is_file() else None


def build_windows_installer(
    app_dir: Path,
    icon_ico: Path,
    version: str,
    output_exe: Path,
    stage: Path,
) -> Path | None:
    """Compile the Inno Setup per-user Windows installer when `iscc` is available."""
    iss_path = write_inno_script(app_dir, icon_ico, version, output_exe,
                                 stage / f'{APP_NAME}.iss')
    iscc = find_inno_compiler()
    if iscc is None:
        return None
    subprocess.run([iscc, str(iss_path)], check=True)
    return output_exe


def build_macos_dmg(app_bundle: Path, dmg_path: Path) -> Path:
    """Create a compressed macOS `.dmg` with an Applications drag-and-drop symlink."""
    dmg_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / 'dmg'
        src.mkdir()
        subprocess.run(['ditto', str(app_bundle), str(src / app_bundle.name)], check=True)
        (src / 'Applications').symlink_to('/Applications')
        subprocess.run(['hdiutil', 'create', '-volname', PRODUCT_NAME,
                        '-srcfolder', str(src), '-ov', '-format', 'UDZO', str(dmg_path)],
                       check=True)
    return dmg_path


def _run_flet_pack(system: str, stage: Path, version: str, icon_path: Path | None) -> None:
    sibling = Path(sys.executable).with_name('flet.exe' if os.name == 'nt' else 'flet')
    flet = str(sibling) if sibling.is_file() else shutil.which('flet')
    if flet is None:
        raise RuntimeError('flet CLI is missing; install the gui-pack or gui-test extra')
    launcher = [flet] if os.name == 'nt' else [sys.executable, flet]
    env = dict(os.environ)
    env['PYTHONPATH'] = os.pathsep.join(filter(None, [str(ROOT), env.get('PYTHONPATH')]))
    if system == 'linux':
        env.setdefault('FLET_LINUX_DISTRO', 'ubuntu22.04')
    cmd = [
        *launcher, 'pack', str(ROOT / 'desktop' / 'main.py'),
        '--name', APP_NAME, '--product-name', PRODUCT_NAME,
        '--product-version', version, '--bundle-id', BUNDLE_ID,
        '--distpath', str(stage), '--yes',
    ]
    if icon_path is not None:
        cmd.extend(['--icon', str(icon_path)])
    if system in ('linux', 'windows'):
        cmd.append('--onedir')
    cmd.extend(f'--pyinstaller-build-args=--exclude-module={mod}' for mod in EXCLUDED_MODULES)
    subprocess.run(cmd, cwd=ROOT, env=env, check=True)


def _package_linux(stage: Path, icons: dict[str, Path], output: Path,
                   stem: str, version: str, arch: str) -> list[Path]:
    app_dir = stage / APP_NAME
    desktop = stage / f'{BUNDLE_ID}.desktop'
    icon = stage / f'{BUNDLE_ID}.png'
    if not icon.is_file():
        shutil.copy2(icons['png'], icon)
    if not (app_dir / APP_NAME).is_file() or not desktop.is_file():
        raise RuntimeError(f'flet pack did not produce unpacked app under {app_dir}')
    unpack_bundled_flet_client(app_dir, 'linux')
    return [
        build_linux_tarball(app_dir, desktop, icon, output / f'{stem}.tar.gz'),
        build_linux_appimage(app_dir, desktop, icon, version, output / f'{stem}.AppImage'),
        build_linux_deb(app_dir, desktop, icon, version, arch, output / f'{stem}.deb'),
    ]


def _package_windows(stage: Path, icons: dict[str, Path], output: Path,
                     stem: str, version: str) -> list[Path]:
    app_dir = stage / APP_NAME
    if not (app_dir / f'{APP_NAME}.exe').is_file():
        raise RuntimeError(f'flet pack did not produce {app_dir / APP_NAME}.exe')
    unpack_bundled_flet_client(app_dir, 'windows')
    artifacts = [build_windows_zip(app_dir, output / f'{stem}.zip')]
    installer = build_windows_installer(
        app_dir, icons['ico'], version, output / f'{stem}-setup.exe', stage,
    )
    if installer is not None:
        artifacts.append(installer)
    return artifacts


def _package_macos(stage: Path, output: Path, stem: str) -> list[Path]:
    app = stage / f'{APP_NAME}.app'
    if not app.is_dir():
        raise RuntimeError(f'flet pack did not produce {app}')
    zip_path = output / f'{stem}.zip'
    subprocess.run(['ditto', '-c', '-k', '--sequesterRsrc', '--keepParent',
                    str(app), str(zip_path)], check=True)
    return [zip_path, build_macos_dmg(app, output / f'{stem}.dmg')]


def build(version: str, output: Path, arch: str | None = None) -> list[Path]:
    """Build all desktop distribution artifacts for the current operating system."""
    system = platform.system().lower()
    if system not in ('linux', 'windows', 'darwin'):
        raise RuntimeError(f'unsupported desktop platform: {system}')
    label = {'darwin': 'macos', 'windows': 'windows', 'linux': 'linux'}[system]
    machine = platform.machine().lower()
    target_arch = arch or {'amd64': 'x86_64', 'x64': 'x86_64', 'aarch64': 'arm64'}.get(
        machine, machine)
    stage = ROOT / 'build' / 'desktop-pack'
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True, exist_ok=True)

    try:
        with tempfile.TemporaryDirectory(prefix='reclaim-icons-') as icon_tmp:
            icons = write_icons(Path(icon_tmp))
            _run_flet_pack(system, stage, version,
                           icons['png'] if system == 'linux' else None)

            output.mkdir(parents=True, exist_ok=True)
            stem = f'{ARCHIVE_NAME}-{version}-{label}-{target_arch}'
            if system == 'linux':
                artifacts = _package_linux(stage, icons, output, stem, version, target_arch)
            elif system == 'windows':
                artifacts = _package_windows(stage, icons, output, stem, version)
            else:
                artifacts = _package_macos(stage, output, stem)

            for artifact in artifacts:
                if artifact.stat().st_size < 1_000_000:
                    raise RuntimeError(f'desktop archive is unexpectedly small: {artifact}')
            return artifacts
    finally:
        shutil.rmtree(ROOT / 'build', ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', required=True)
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'dist' / 'release')
    parser.add_argument('--arch', default=None)
    args = parser.parse_args()
    if not re.fullmatch(r'\d+\.\d+\.\d+', args.version):
        parser.error('version must be X.Y.Z')
    for path in build(args.version, args.output_dir.resolve(), arch=args.arch):
        print(path)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
