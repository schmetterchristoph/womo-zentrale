#!/usr/bin/env python3
"""Hilfswerkzeug: findet die SHR-Steuerung per Bluetooth und listet ihre
Dienste und Merkmale (Services/Characteristics) auf.

Aufruf auf dem Raspberry Pi (SHR-App auf dem Handy vorher trennen!):
    python3 tools/shr_scan.py            # alle Bluetooth-Geräte in der Nähe
    python3 tools/shr_scan.py AA:BB:...  # Details eines Geräts
Die Ausgabe bitte an Claude schicken.
"""
import asyncio
import sys
from bleak import BleakClient, BleakScanner


async def scan():
    print("Suche 10 Sekunden nach Bluetooth-Geräten ...")
    found = await BleakScanner.discover(timeout=10, return_adv=True)
    for addr, (d, ad) in sorted(found.items(), key=lambda x: -x[1][1].rssi):
        print(f"{addr}  RSSI {ad.rssi:4d}  Name: {d.name or '-'}  Dienste: {', '.join(ad.service_uuids) or '-'}")


async def detail(addr):
    async with BleakClient(addr, timeout=20) as c:
        print(f"Verbunden mit {addr}")
        for s in c.services:
            print(f"\nDienst {s.uuid}  ({s.description})")
            for ch in s.characteristics:
                print(f"  Merkmal {ch.uuid}  Eigenschaften: {','.join(ch.properties)}")
                if "read" in ch.properties:
                    try:
                        print(f"    Wert: {(await c.read_gatt_char(ch)).hex(' ')}")
                    except Exception as e:
                        print(f"    (nicht lesbar: {e})")


if __name__ == "__main__":
    asyncio.run(detail(sys.argv[1]) if len(sys.argv) > 1 else scan())
