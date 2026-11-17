"""Find your Centauri Carbon 2 on the LAN and print its address + serial for config.json.
python find_printer.py            (the PC must be on the same network as the printer)"""
import cc2

ip = cc2.find_printer("")
if not ip:
    raise SystemExit("No printer answered. Is it on, on the same Wi-Fi/LAN, and is LAN mode enabled?")
info = cc2.discover(ip)
print("printer host:", ip)
print("identity:", info)
