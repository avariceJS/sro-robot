#!/bin/zsh
set -eu
script_dir=${0:A:h}
repository_dir=${script_dir:h:h:h}
exec "$repository_dir/.venv/bin/python" "$script_dir/download_current_kad.py"
