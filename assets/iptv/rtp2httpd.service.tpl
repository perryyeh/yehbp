[Unit]
Description=rtp2httpd IPTV multicast-to-HTTP service (YehBP)
Wants=NetworkManager-wait-online.service
After=NetworkManager-wait-online.service

[Service]
Type=simple
WorkingDirectory=__APP_DIR__
ExecStartPre=/usr/bin/nmcli --wait 60 connection up uuid __MULTICAST_PROFILE_UUID__
ExecStartPre=/usr/bin/nmcli --wait 60 connection up uuid __FCC_PROFILE_UUID__
ExecStart=__APP_DIR__/rtp2httpd -c __CONFIG_PATH__
Restart=on-failure
RestartSec=5
TimeoutStopSec=20

[Install]
WantedBy=multi-user.target
