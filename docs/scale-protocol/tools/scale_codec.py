"""
scale_codec.py - Complete codec and checksum implementation for Fitdays / Lefu scale (JEETIF2421).
Reverse-engineered and mathematically verified against 814 captured frames across all 9 frame types.
"""

import struct
import time

def compute_checksum(ftype: int, payload: bytes) -> int:
    """
    Computes the 6-bit frame checksum.
    
    Verified across 814 frames (100.000% match):
      - Low 5 bits (0x1F): (TYPE + sum(payload)) & 0x1F (additive modulo 32)
      - Bit 5 (0x20):
          * 0 (0x00) for 0xA2 (live streaming weight notifications on FFB2)
          * 1 (0x20) for all other frames (commands 0xB0, 0xB6, profile 0xC0, 0xC1,
            and responses/indications 0xA0, 0xA5, 0xA7, 0xAA)
      - Bits 6-7: always 0 (max checksum is 0x3F)
    """
    ck_low = (ftype + sum(payload)) & 0x1F
    return ck_low if (ftype == 0xA2) else (ck_low | 0x20)

def make_frame(seq: int, ftype: int, payload: bytes) -> bytes:
    """
    Builds a complete BLE frame with envelope and verified checksum:
      [0] SEQ
      [1] 0x00
      [2] LEN (len(payload) + 1, big-endian low byte)
      [3] 0x00
      [4] TYPE
      [5..] payload
      [-1] CHECKSUM
    """
    length = len(payload) + 1
    ck = compute_checksum(ftype, payload)
    return bytes([seq & 0xFF, 0x00, length, 0x00, ftype & 0xFF]) + payload + bytes([ck])

def parse_frame(frame: bytes):
    """
    Parses and validates a frame.
    Returns dict with parsed fields and validation status.
    """
    if len(frame) < 6:
        raise ValueError(f"Frame too short ({len(frame)} bytes)")
    seq = frame[0]
    flen = frame[2]
    ftype = frame[4]
    payload = frame[5:-1]
    ck = frame[-1]
    expected_len = flen + 5
    if len(frame) != expected_len:
        raise ValueError(f"Length mismatch: header says {expected_len}, got {len(frame)}")
    expected_ck = compute_checksum(ftype, payload)
    valid_ck = (ck == expected_ck)
    
    res = {
        'seq': seq,
        'type': ftype,
        'payload': payload,
        'checksum': ck,
        'expected_checksum': expected_ck,
        'valid_checksum': valid_ck,
    }
    
    if ftype == 0xA2 and len(payload) >= 6:
        # A2 live weight: STATUS, 00, W_HI, W_MID, W_LO, ??
        status = payload[0]
        weight_raw = struct.unpack('>I', b'\x00' + payload[2:5])[0]
        res['status'] = status
        res['weight_kg'] = weight_raw / 1000.0
    elif ftype in (0xA5, 0xA7) and len(payload) >= 34:
        # A7 / A5 composition: ts(4), 25(1), weight_u24(3), 00 0A(2), 10x u16(20), device_id(4)
        ts, code_25 = struct.unpack('>IB', payload[0:5])
        weight_raw = struct.unpack('>I', b'\x00' + payload[5:8])[0]
        imp_words = struct.unpack('>10H', payload[10:30])
        imp_ohms = [w / 10.0 for w in imp_words]
        # Layout: slots 0-4 are frequency 1, slots 5-9 the SAME segments at
        # frequency 2 (NOT interleaved - see README section 10).
        # Order: [trunk, L arm, R arm, L leg, R leg]. Arms confirmed against the
        # app's own segmental figures; leg left/right is inferred from the
        # arms' ordering and is NOT independently verified.
        res['segments'] = {
            name: (imp_ohms[i], imp_ohms[i + 5])
            for i, name in enumerate(["trunk", "arm_left", "arm_right",
                                      "leg_left", "leg_right"])
        }
        dev_id = payload[30:34]
        res['timestamp'] = ts
        res['weight_kg'] = weight_raw / 1000.0
        res['impedance_raw_words'] = imp_words
        res['impedance_ohms'] = imp_ohms
        res['device_id'] = dev_id.hex()
        res['impedance_block_hex'] = payload[10:30].hex()
    elif ftype == 0xAA and len(payload) >= 28:
        # Scale hello / advertise capabilities
        res['scale_token'] = payload[-5:]
        
    return res

def build_c0_profile(seq: int, timestamp: int, height_cm: int, weight_kg: float,
                     user_id: bytes = bytes.fromhex("124de8bf"),
                     name: str = "Dan", gender: int = 1, tz_offset_min: int = 60) -> bytes:
    """
    Builds a Type 0xC0 profile push frame with current timestamp and dynamic checksum.
    """
    # Weight low 16 bits in grams
    weight_grams = int(round(weight_kg * 1000))
    w_u16 = weight_grams & 0xFFFF
    
    # Payload format:
    # ts (4B BE)
    # tz_offset_min (2B BE, e.g. 0x003c = 60 min)
    # gender (1B, 0x01 = male)
    # height_cm (1B, e.g. 0xb9 = 185)
    # flag/age_code (1B, 0x1c = 28)
    # weight_u16 (2B BE)
    # 1c 25 (2B)
    # 1d 6a (2B)
    # 0f (1B)
    # user_id (4B, 12 4d e8 bf)
    # 01 01 03 (3B)
    # name (ASCII, padded to 3 bytes if short)
    name_bytes = name.encode('ascii')[:3].ljust(3, b'\x00')
    
    payload = struct.pack('>IHBBBH', timestamp, tz_offset_min, gender, height_cm, 0x1C, w_u16)
    payload += bytes.fromhex("1c251d6a0f")
    payload += user_id
    payload += bytes([0x01, 0x01, 0x03])
    payload += name_bytes
    
    return make_frame(seq, 0xC0, payload)

def build_c1_profile(seq: int, height_cm: int, weight_kg: float,
                     user_id: bytes = bytes.fromhex("124de8bf"),
                     name: str = "Dan", gender: int = 1) -> bytes:
    """
    Builds a Type 0xC1 profile variant (no timestamp) with dynamic checksum.
    """
    weight_grams = int(round(weight_kg * 1000))
    w_u16 = weight_grams & 0xFFFF
    name_bytes = name.encode('ascii')[:3].ljust(3, b'\x00')
    
    payload = bytes([0x01, gender, height_cm, 0x1C]) + struct.pack('>H', w_u16)
    payload += bytes.fromhex("1c251d6a0f")
    payload += user_id
    payload += bytes([0x01, 0x01, 0x03])
    payload += name_bytes
    
    return make_frame(seq, 0xC1, payload)

def build_b6_trigger(seq: int, token: bytes = bytes.fromhex("0000034000")) -> bytes:
    """
    Builds the B6 trigger command with correct checksum.
    """
    return make_frame(seq, 0xB6, token)

def build_b0_cmd(seq: int, cmd: int) -> bytes:
    """
    Builds B0 control requests (0x30, 0x31, 0x39, 0x3A).
    """
    return make_frame(seq, 0xB0, bytes([cmd & 0xFF, 0x00]))


def compute_body_composition(
    weight_kg: float,
    impedances: list,
    height_cm: float = 185,
    age: int = 38,
    sex: int = 1,
    athlete: bool = True
) -> dict:
    """
    Computes body composition from impedance ohms using WLA37 algorithm.
    """
    from wla37 import WLA37Calculator
    calc = WLA37Calculator()
    return calc.calculate(
        weight_kg=weight_kg,
        height_cm=height_cm,
        sex=sex,
        age=age,
        impedances=impedances,
        athlete=athlete
    )
