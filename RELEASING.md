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
