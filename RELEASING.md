# Releasing (maintainer notes)

## Every release
1. Bump `VERSION` in `mbox_control.py` (and `pkgver` in `packaging/aur/PKGBUILD`).
2. Add a `## X.Y.Z` section to `CHANGELOG.md` — it becomes the release notes.
3. Commit and push to `main`.

GitHub runs the tests (Ubuntu + Arch with real PipeWire). If both pass and `vX.Y.Z` doesn't exist
yet, the release job tags the commit, publishes the release with the changelog section, and attaches a
`PKGBUILD` with the version and checksum filled in. Apps with update checks on are offered it within a day.

## AUR
1. Download the `PKGBUILD` attached to the GitHub release.
2. `makepkg --printsrcinfo > .SRCINFO && makepkg -si` to test it.
3. First time only: `git clone ssh://aur@aur.archlinux.org/mbox-control.git`. This needs an AUR
   account with your SSH key added.
4. Copy `PKGBUILD` and `.SRCINFO` into that clone, then commit and push.

## Regenerating the system files

`packaging/linux/*` (root helper, wake recovery script, polkit policy, sleep-hook template) are generated
from constants in `mbox_control.py`. After changing those, regenerate them; `tests/test_420.py` fails if
they drift:

```sh
QT_QPA_PLATFORM=offscreen python3 -c 'import mbox_control as mc, pathlib as p; d = p.Path("packaging/linux"); \
(d/"mbox-reload-driver").write_text(mc.RELOAD_HELPER); (d/"mbox-wake-recover").write_text(mc.WAKE_RECOVER); \
(d/"org.fancybutler.mboxcontrol.policy").write_text(mc.polkit_policy("/usr/lib/mbox-control/mbox-reload-driver")); \
(d/"mbox-control.sleep-hook").write_text(mc.sleep_hook_text("/usr/lib/mbox-control/mbox-wake-recover"))'
```
