import asyncio, time
from bleak import BleakClient, BleakScanner
ADDR="78:66:A5:72:C4:64"
FFB1="0000ffb1-0000-1000-8000-00805f9b34fb"
FFB2="0000ffb2-0000-1000-8000-00805f9b34fb"
FFB3="0000ffb3-0000-1000-8000-00805f9b34fb"
STALE="01260c050c550ac20b0300dd0a6b0ad709600998"
SEQ=[ "00000300b0300020",
 "01001b00c06aa9a5cd003c01b91c16a61c251d6a0f124de8bf01010344616e28",
 "02001600c10101b91c16a61c251d6a0f124de8bf01010344616e29",
 "03001b00c06aa9a5cd003c01b91c16a61c251d6a0f124de8bf01010344616e28",
 "04000600b6000003400039",
 "05001b00c06aa9a5cd003c01b91c16a61c251d6a0f124de8bf01010344616e28",
 "06001600c10101b91c16a61c251d6a0f124de8bf01010344616e29",
 "07001b00c06aa9a5cd003c01b91c16a61c251d6a0f124de8bf01010344616e28",
 "08000300b0310021",
 "09000300b0390029" ]
log=open("run2.log","w")
def rec(s,d):
    d=bytes(d); t=""
    if len(d)>4 and d[4] in (0xa5,0xa7):
        blk=d[15:35].hex()
        t=" *** FRESH BLOCK ***" if blk!=STALE else " (stale)"
        t=f" type={d[4]:02x}{t} block={blk}"
    line=f"{time.strftime('%H:%M:%S')} {s} len={len(d):3d} {d.hex()}{t}"
    print(line,flush=True); log.write(line+"\n"); log.flush()

async def main():
    deadline=time.time()+600
    while time.time()<deadline:
        try:
            dev=await BleakScanner.find_device_by_address(ADDR,timeout=8)
            if not dev: continue
            async with BleakClient(dev,timeout=40) as c:
                await c.start_notify(FFB3, lambda h,x: rec("FFB3-ind ",x))
                await c.start_notify(FFB2, lambda h,x: rec("FFB2-noti",x))
                print(f"{time.strftime('%H:%M:%S')} CONNECTED - replaying {len(SEQ)} FFB1 writes",flush=True)
                for i,h in enumerate(SEQ):
                    try:
                        await c.write_gatt_char(FFB1, bytes.fromhex(h), response=True)
                        print(f"   -> [{i}] {h}",flush=True)
                    except Exception as e:
                        print(f"   !! [{i}] {type(e).__name__} {e}",flush=True)
                    await asyncio.sleep(0.22)
                print(f"{time.strftime('%H:%M:%S')} replay done - STAND ON SCALE",flush=True)
                while c.is_connected and time.time()<deadline:
                    await asyncio.sleep(1)
        except Exception as e:
            print(f"{time.strftime('%H:%M:%S')} {type(e).__name__}",flush=True)
        await asyncio.sleep(0.5)
asyncio.run(main())
