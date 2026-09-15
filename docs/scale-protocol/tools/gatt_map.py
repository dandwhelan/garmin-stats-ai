import asyncio
from bleak import BleakClient, BleakScanner
ADDR="78:66:A5:72:C4:64"

async def main():
    print("finding device...")
    dev = await BleakScanner.find_device_by_address(ADDR, timeout=25)
    if not dev:
        print("NOT FOUND - scale may be asleep"); return
    print("found, connecting...")
    async with BleakClient(dev, timeout=30) as c:
        print("CONNECTED\n")
        for s in c.services:
            print(f"SERVICE {s.uuid}  ({s.description})")
            for ch in s.characteristics:
                print(f"  CHAR {ch.uuid}  handle={ch.handle}  props={','.join(ch.properties)}")
                for d in ch.descriptors:
                    print(f"      DESC {d.uuid} handle={d.handle}")
asyncio.run(main())
