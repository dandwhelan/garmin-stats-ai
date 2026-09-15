from collect_frames import unique_frames
import scale_codec, time

seen = set()
for f in unique_frames:
    if f[4] in (0xa5, 0xa7):
        p = scale_codec.parse_frame(f)
        ts = p['timestamp']
        w = p['weight_kg']
        blk = p['impedance_block_hex']
        if blk in seen: continue
        seen.add(blk)
        t_str = time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(ts))
        ohms = p['impedance_ohms']
        ptype = p['type']
        print(f"Type 0x{ptype:02x} {t_str} UTC: W={w:.3f} kg, blk={blk}")
        print(f"   f1: trunk={ohms[0]:.1f}, la={ohms[1]:.1f}, ra={ohms[2]:.1f}, ll={ohms[3]:.1f}, rl={ohms[4]:.1f}")
        print(f"   f2: trunk={ohms[5]:.1f}, la={ohms[6]:.1f}, ra={ohms[7]:.1f}, ll={ohms[8]:.1f}, rl={ohms[9]:.1f}")
