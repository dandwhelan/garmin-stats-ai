import asyncio, time
from bleak import BleakClient, BleakScanner
ADDR="78:66:A5:72:C4:64"
FFB2="0000ffb2-0000-1000-8000-00805f9b34fb"
FFB3="0000ffb3-0000-1000-8000-00805f9b34fb"
t0=time.time(); log=open("watch.log","a")
def rec(src,d):
    d=bytes(d)
    tag="*** A7 ***" if len(d)>4 and d[4]==0xa7 else ""
    line=f"{time.strftime('%H:%M:%S')} {src} len={len(d):3d} {d.hex()} {tag}"
    print(line,flush=True); log.write(line+"\n"); log.flush()
async def session():
    dev=await BleakScanner.find_device_by_address(ADDR,timeout=15)
    if not dev: return
    async with BleakClient(dev,timeout=20) as c:
        await c.start_notify(FFB3, lambda h,d: rec("FFB3-ind ",d))
        await c.start_notify(FFB2, lambda h,d: rec("FFB2-noti",d))
        print(f"{time.strftime('%H:%M:%S')} CONNECTED",flush=True)
        while c.is_connected: await asyncio.sleep(1)
    print(f"{time.strftime('%H:%M:%S')} disconnected",flush=True)
async def main():
    while True:
        try: await session()
        except Exception as e: print(f"{time.strftime('%H:%M:%S')} retry {type(e).__name__}",flush=True)
        await asyncio.sleep(2)
asyncio.run(main())
