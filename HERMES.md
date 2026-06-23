# YehBP maintenance notes

- Project identity: repository `https://github.com/perryyeh/yehbp`, installed command `yehbp`, title `Yeh Bypass (Gateway)`, personal bypass-router helper covering DNS cache/splitting, proxy access, and remote home-network access.
- README install command should stay as a simple pipe form: `curl -fsSL <install.sh> | sudo bash`; do not change it to process substitution or require extra parameters.
- Version numbers use `YYYY.MM.DD.NN` based on the current local date, not the previous version's date. If today's date changed, reset the suffix to `.01`; otherwise increment the suffix. README-only documentation changes do not require a version bump.
- Keep `VERSION` and `APP_VERSION` in `install.sh` identical whenever a version bump is needed.
- Run syntax checks before pushing: `bash -n install.sh`, `bash -n assets/docker-auto-update/docker-auto-update.sh`, and `python3 -m py_compile assets/docker-auto-update/check-compose-macs.py` when those files exist.
- Commit and push validated functional/script changes to `perryyeh/yehbp`.
- Larger helper scripts/templates belong under `assets/<feature>/`; `install.sh` should download/render them instead of embedding large heredocs.
- Docker auto-update installs under the selected `dockerapps/_auto_update` via menu items; do not embed large auto-update payloads as heredocs in `install.sh`.
- The Dockcheck auto-update feature uses menu 96 install, 97 cleanup, 98 manual check/update. Avoid reintroducing Watchtower menu entries.
- Install Dockcheck from `https://raw.githubusercontent.com/mag37/dockcheck/main/dockcheck.sh` first; keep `assets/docker-auto-update/dockcheck.sh` only as fallback.
- Menu 98 should locate the installation via systemd `ExecStart` only.
- The auto-update wrapper should resolve config from its own directory, not hardcoded paths.
- yehbp upgrades should overwrite `/usr/local/bin/yehbp` without creating `.bak-*` files, and update checks must ignore remote versions that are not strictly greater than the local version.
- Docker app installers should match existing replacement semantics: if the target app directory already exists under the selected dockerapps path, treat it as a replacement install and move/replace the old directory consistently with other app installers. Do not silently merge or preserve app state unless explicitly requested.
- For changes limited to the yehbp repository, verify locally only; do not use n350/r76s or other remote hosts for validation or temporary testing unless explicitly requested.
