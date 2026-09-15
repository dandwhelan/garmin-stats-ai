import struct, os, glob, re

frames = set()

# 1. From pcapng
def parse_pcapng(filename):
    if not os.path.exists(filename): return []
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

    frags = {}
    att = []
    for ts, p, cap, orig in pkts:
        if len(p) < 5: continue
        dirn = struct.unpack('>I', p[:4])[0] # 0=TX, 1=RX
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

    collected = []
    for tt, dr, p in att:
        op = p[0]
        if op in (0x1b, 0x1d, 0x52, 0x12):
            h = struct.unpack('<H', p[1:3])[0]; v = p[3:]
            # Check if looks like frame: len >= 5 and v[1] == 0 and v[3] == 0
            if len(v) >= 5 and v[1] == 0 and v[3] == 0:
                expected_len = v[2] + 5
                if len(v) == expected_len:
                    collected.append(('pcapng', 'TX' if dr == 0 else 'RX', h, v))
    return collected

pcap_frames = parse_pcapng('C:/Users/DanW/Scales/capture.pcapng')
print(f"Loaded {len(pcap_frames)} frames from pcapng")

# 2. From logs
hex_pat = re.compile(r'\b([0-9a-fA-F]{10,})\b')
log_files = glob.glob('C:/Users/DanW/Scales/scale-protocol/captures/*.log') + glob.glob('C:/Users/DanW/Scales/scale-protocol/captures/*.out')
log_frames = []
for lf in log_files:
    for line in open(lf, errors='ignore'):
        for m in hex_pat.finditer(line):
            h = m.group(1).lower()
            if len(h) % 2 == 0:
                raw = bytes.fromhex(h)
                if len(raw) >= 5 and raw[1] == 0 and raw[3] == 0:
                    exp_len = raw[2] + 5
                    if len(raw) == exp_len:
                        log_frames.append((os.path.basename(lf), raw))

print(f"Loaded {len(log_frames)} frames from log files")

all_records = []
for src, dr, h, v in pcap_frames:
    all_records.append((src, dr, v))
for src, v in log_frames:
    all_records.append((src, '?', v))

unique_frames = {}
for src, dr, v in all_records:
    if v not in unique_frames:
        unique_frames[v] = (src, dr)

print(f"Total unique full frames: {len(unique_frames)}")

by_type = {}
for v, (src, dr) in unique_frames.items():
    ftype = v[4]
    flen = len(v)
    by_type.setdefault((ftype, flen), []).append(v)

for (ftype, flen), lst in sorted(by_type.items()):
    print(f"Type 0x{ftype:02X}, Len {flen:2d}: {len(lst)} frames")
    for f in lst[:3]:
        print(f"   {f.hex()}")
