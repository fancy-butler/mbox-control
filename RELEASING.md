# Releasing (maintainer notes)

## First time
1. Create an empty public repository called `mbox-control` on GitHub.
2. Inside this folder:
   ```
   git remote add origin https://github.com/<your-username>/mbox-control.git
   git push -u origin main --tags
   ```
3. On GitHub, open **Releases → Draft a new release**, pick tag `v4.12.1`, and paste that
   version's section from CHANGELOG.md.

## Every release
1. Bump `VERSION` in `mbox_control.py` and add a section to `CHANGELOG.md`.
2. Commit, then `git tag vX.Y.Z && git push && git push --tags`, then make the GitHub release.

## AUR
1. In `packaging/aur/PKGBUILD`, set `_ghuser` and `pkgver`, and reset `pkgrel=1`.
2. `updpkgsums && makepkg --printsrcinfo > .SRCINFO && makepkg -si` to test it.
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
