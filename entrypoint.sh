#!/bin/sh
# HHS Clips entrypoint: keep accounts + session secret in the /state
# volume so they survive `docker compose up --build` (the container's
# own filesystem is wiped on rebuild, volumes are not).
set -u
mkdir -p /state

for f in accounts.json .session-secret; do
  if [ -f "/state/$f" ]; then
    cp "/state/$f" "/app/$f"
  fi
done

save_state() {
  for f in accounts.json .session-secret; do
    if [ -f "/app/$f" ]; then
      cp "/app/$f" "/state/$f"
    fi
  done
}

python3 server.py --host 0.0.0.0 --port 8000 &
pid=$!

# On stop (SIGTERM from `docker compose stop/down`), stop the server
# cleanly, then save state before exiting.
trap 'kill $pid 2>/dev/null; wait $pid 2>/dev/null; save_state; exit 0' TERM INT

wait $pid
save_state
