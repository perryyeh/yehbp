[Unit]
Description=Run YehBP Docker Compose auto update daily

[Timer]
OnCalendar=__TIMER_CALENDAR__
Persistent=true
RandomizedDelaySec=300
Unit=yehbp-docker-auto-update.service

[Install]
WantedBy=timers.target
