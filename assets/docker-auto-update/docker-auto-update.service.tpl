[Unit]
Description=YehBP Docker Compose auto update via Dockcheck
Wants=network-online.target docker.service
After=network-online.target docker.service

[Service]
Type=oneshot
ExecStart=__BASE_DIR__/docker-auto-update.sh
