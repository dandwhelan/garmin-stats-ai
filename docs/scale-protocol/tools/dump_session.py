import struct, os

filename = 'C:/Users/DanW/Scales/capture.pcapng'
d = open(filename, 'rb').read()
off = 0; pkts = []
while off + 8 <= len(d):
    btype, blen = struct.unpack('<II', d[off:off+8])
    if blen < 12 or off + blen > len(d): break
    body = d[off+8:off+blen-4]
    if btype == 0x06:
        iid, th, tl, cap, orig = struct.unpack('<IIIII', body[:20])
        ts = (th << 32) | tl
        pkts.append((ts, body[20:20+cap], cap, orig))
    off += blen

t0 = pkts[0][0]
frags = {}
att = []
for ts, p, cap, orig in pkts:
    if len(p) < 5: continue
    dirn = struct.unpack('>I', p[:4])[0]
    h4 = p[4:]
    if not h4 or h4[0] != 2: continue
    b = h4[1:]
    if len(b) < 4: continue
    hh, dl = struct.unpack('<HH', b[:4]); hnd = hh & 0xfff; pb = (hh >> 12) & 3
    data = b[4:4+dl]
    if pb in (0, 2): frags[hnd] = [bytearray(data), dirn, ts]
    elif hnd in frags: frags[hnd][0] += data
    else: continue
    buf, dr, tt = frags[hnd]
    if len(buf) >= 4:
        l2, cid = struct.unpack('<HH', bytes(buf[:4]))
        if len(buf) - 4 >= l2:
            pl = bytes(buf[4:4+l2]); del frags[hnd]
            if cid == 4 and pl: att.append((tt, dr, pl))

OP = {0x1b:'NOTIFY', 0x1d:'INDICATE', 0x52:'WRITE_CMD', 0x12:'WRITE_REQ', 0x13:'WRITE_RSP', 0x1e:'CONFIRM'}
for tt, dr, p in att:
    op = p[0]
    rel = (tt - t0) / 1e6
    if op in OP:
        h = struct.unpack('<H', p[1:3])[0] if len(p) >= 3 else 0
        v = p[3:] if len(p) >= 3 else b''
        v_str = f"h=0x{h:02x}({h:2d}) v={v.hex()}" if v else ""
        d_str = "TX (app->scale)" if dr == 0 else "RX (scale->app)"
        print(f"{rel:7.3f}s {d_str:17s} {OP[op]:9s} {v_str}")
