#!/data/data/com.termux/files/usr/bin/sh
# Keeps the printer link running on an Android tablet (Termux): restarts it 10 s after it stops
# (a crash, or the app's "Restart bridge" button). Copy the bridge/ folder to ~/mpc first.
cd ~/mpc
if [ -f run.pid ] && kill -0 "$(cat run.pid)" 2>/dev/null; then echo "already running"; exit 0; fi
echo $$ > run.pid
termux-wake-lock
while true; do
  python bridge.py --role link >> run.log 2>&1
  tail -c 2000000 run.log > run.log.tmp && mv run.log.tmp run.log
  sleep 10
done
