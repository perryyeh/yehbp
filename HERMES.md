# YehBP maintenance notes

- Version numbers use `YYYY.MM.DD.NN` based on the current local date, not the previous version's date. If today's date changed, reset the suffix to `.01`; otherwise increment the suffix.
- Keep `VERSION` and `APP_VERSION` in `install.sh` identical for every released change.
- Run syntax checks before pushing: `bash -n install.sh`, `bash -n assets/docker-auto-update/docker-auto-update.sh`, and `python3 -m py_compile assets/docker-auto-update/check-compose-macs.py` when those files exist.
- Larger helper scripts/templates belong under `assets/<feature>/`; `install.sh` should download/render them instead of embedding large heredocs.
- The Dockcheck auto-update feature uses menu 96 install, 97 cleanup, 98 manual check/update. Avoid reintroducing Watchtower menu entries.
- yehbp upgrades should overwrite `/usr/local/bin/yehbp` without creating `.bak-*` files, and update checks must ignore remote versions that are not strictly greater than the local version.
