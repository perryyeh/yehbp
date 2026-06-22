# yehbp docker compose auto update config
ROOT_DIR=__ROOT_DIR__
BASE_DIR=__BASE_DIR__
LOG_DIR=__LOG_DIR__
# Keep only recent update logs; 7 means delete update-*.log older than 6 full days.
LOG_RETENTION_DAYS=7
# Extra dockcheck options. Keep automatic mode in wrapper; do not put -a/-n here.
DOCKCHECK_EXTRA_ARGS="-m -t 30"
# Set to true to prune dangling images after successful updates.
AUTO_PRUNE=__AUTO_PRUNE__
# Delay new images by N days before update; 0 disables delay.
DELAY_DAYS=__DELAY_DAYS__
# Check compose mac_address against actual Docker endpoint after run.
CHECK_MAC=true
