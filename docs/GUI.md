# Desktop GUI

The Flet window manages archive updates for all six native providers. On
startup it reads local files only: archived chat counts, last manifest time,
and the last run result. It shows live changes only after a dry run or update
contacts a provider.

Use **Update selected** for checked providers or **Update all** for every
provider enabled in settings. **Dry run** lists current provider chats and
previews the work without writing archive files. **Rebuild selected** asks for
confirmation, then fetches all selected chats again. The progress bar and
Details panel show the operation; provider errors appear in the summary and
details. Playwright work runs in a worker thread, keeping the window
responsive while login or downloads take time.

**Settings** writes the shared `.reclaim.json`. It can choose enabled
providers, an archive directory, and an existing browser profile. The profile
field is useful when a standalone download should reuse a checkout's
`.playwright-profile`; point it at that folder. The field stores a path, not
account passwords. Close any other Reclaim process using that profile before
running the GUI. A newly installed desktop download otherwise creates its
own profile in the operating system data folder described in the README.

## Run from source

```bash
python -m pip install -e '.[gui]'
reclaim gui
# equivalent direct entry point:
reclaim-gui
```

`reclaim gui` reports how to install the extra if Flet is absent. On Linux,
the directory chooser uses `zenity`; the path can always be typed directly.
Chrome or Chromium must be available on the machine or in Playwright's
browser cache. The desktop package does not include Chromium.

## Test and package

```bash
python -m pip install -e '.[gui-test]' build ruff pyright
python -m unittest discover -s tests -q
ruff check reclaim tests scripts flet_integration
pyright reclaim tests scripts flet_integration
flet test linux flet_integration --tests-dir tests --yes
python scripts/pack_desktop.py --version 0.0.0
```

The Flet device tests run the real controls with a deterministic fake
controller. Linux device tests require Flutter, a display, and the packages
listed in [Flet's Linux build
requirements](https://flet.dev/docs/publish/linux/). The separate `flet pack`
build uses PyInstaller. Launch the resulting executable outside the Python
environment to catch import and packaged-path problems; the device tests do
not test the PyInstaller binary.

The Linux archive also contains a `.desktop` template. If you install it in
your application menu, set its `Exec=` line to the absolute path of the
extracted `ReclaimMyChats` executable first.
