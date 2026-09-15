"""Fitdays' own body-composition model (``ICBodyFatAlgorithmWLA37``).

Runs the vendor's ``libICBodyFatAlgorithms.so`` (ARM64, extracted from the
Fitdays APK) under Unicorn emulation, turning the scale's ten segmental
impedances into the same figures the Fitdays app shows. Verified against a
paired app weigh-in in ``docs/scale-protocol/README.md`` §11.7.

The binary is **proprietary and never committed**. It is looked up at
``WLA37_SO_PATH`` or ``<repo>/vendor/libICBodyFatAlgorithms.so``; when it (or
the optional ``unicorn``/``pyelftools`` deps) is missing, :func:`compute_wla37`
returns ``None`` and callers fall back to the generic estimate.

Why emulation even on the Pi: the ``.so`` links against Android's bionic libc
(``__sF``, ``__system_property_get``), so ``dlopen`` fails under glibc.
"""

from __future__ import annotations

import logging
import math
import os
import struct
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["compute_wla37", "engine_status"]

_SYMBOL = "_ZN23ICBodyFatAlgorithmWLA374calcE28__ICBodyFatAlgorithmParams__"
_REPO_ROOT = Path(__file__).resolve().parents[4]
_RESULT_SIZE = 588
_SEGMENTS = ("trunk", "left_arm", "right_arm", "left_leg", "right_leg")
# Output offsets per segment: fat %, fat kg, muscle %, muscle kg.
_SEGMENT_OFFSETS = {
    "left_leg": 0x50, "right_leg": 0x70, "left_arm": 0x90, "right_arm": 0xB0, "trunk": 0xD0,
}

_lock = threading.Lock()
_engine: "_Emulator | None" = None
_engine_error: str | None = None


def _so_path() -> Path:
    env = os.environ.get("WLA37_SO_PATH", "").strip()
    return Path(env).expanduser() if env else _REPO_ROOT / "vendor" / "libICBodyFatAlgorithms.so"


class _Emulator:
    """Loads the ELF into a Unicorn ARM64 VM once; each call reuses it."""

    def __init__(self, so_path: Path) -> None:
        from elftools.elf.elffile import ELFFile
        from unicorn import UC_ARCH_ARM64, UC_HOOK_CODE, UC_MODE_ARM, Uc
        from unicorn.arm64_const import (
            UC_ARM64_REG_LR, UC_ARM64_REG_PC, UC_ARM64_REG_TPIDR_EL0,
            UC_ARM64_REG_V0, UC_ARM64_REG_V1, UC_ARM64_REG_X0, UC_ARM64_REG_X1,
            UC_ARM64_REG_X2,
        )

        with open(so_path, "rb") as f:
            elf = ELFFile(f)
            segments = [
                (s["p_vaddr"], s["p_memsz"], s["p_filesz"], s.data())
                for s in elf.iter_segments() if s["p_type"] == "PT_LOAD"
            ]
            dynsym = elf.get_section_by_name(".dynsym")
            syms = dict(enumerate(dynsym.iter_symbols()))
            by_name = {s.name: s for s in syms.values() if s.name}
            relocs = list(elf.get_section_by_name(".rela.dyn").iter_relocations())
            relocs += list(elf.get_section_by_name(".rela.plt").iter_relocations())
        self.entry = by_name[_SYMBOL]["st_value"]

        uc = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
        uc.mem_map(0, 0x200000)
        for vaddr, memsz, filesz, data in segments:
            uc.mem_write(vaddr, data)
            if memsz > filesz:
                uc.mem_write(vaddr + filesz, b"\x00" * (memsz - filesz))

        # Every import resolves to a `ret` stub; the few the algorithm really
        # needs are implemented in the code hook below.
        hooks_base, next_hook = 0x90000000, [0x90000000]
        uc.mem_map(hooks_base, 0x10000)

        def stub() -> int:
            addr = next_hook[0]
            next_hook[0] += 16
            uc.mem_write(addr, b"\xc0\x03\x5f\xd6")
            return addr

        fmodf, fmod, memcpy, free = stub(), stub(), stub(), stub()
        named = {"fmodf": fmodf, "fmod": fmod, "memcpy": memcpy, "free": free, "_ZdlPv": free}
        for rel in relocs:
            rtype, offset = rel["r_info_type"], rel["r_offset"]
            if rtype == 1027:  # R_AARCH64_RELATIVE
                uc.mem_write(offset, struct.pack("<Q", rel["r_addend"]))
            elif rtype in (257, 1025, 1026):  # ABS64, GLOB_DAT, JUMP_SLOT
                sym = syms[rel["r_info_sym"]]
                target = sym["st_value"] + rel["r_addend"] if sym["st_value"] else (
                    named.get(sym.name) or stub()
                )
                uc.mem_write(offset, struct.pack("<Q", target))

        stack_base, stack_size = 0x70000000, 0x100000
        uc.mem_map(stack_base, stack_size)
        self.sp = stack_base + stack_size - 0x1000
        uc.mem_map(0x50000000, 0x10000)  # TLS, for the stack canary
        uc.mem_write(0x50000028, b"\x12\x34\x56\x78\x9a\xbc\xde\xf0")
        uc.reg_write(UC_ARM64_REG_TPIDR_EL0, 0x50000000)
        uc.mem_map(0x60000000, 0x10000)
        self.params, self.result = 0x60000000, 0x60001000
        uc.mem_map(0x88880000, 0x10000)
        self.stop = 0x88888888
        uc.mem_write(self.stop, b"\xc0\x03\x5f\xd6")

        def on_code(emu, address, _size, _data):
            ret = lambda: emu.reg_write(UC_ARM64_REG_PC, emu.reg_read(UC_ARM64_REG_LR))  # noqa: E731
            if address == fmodf:
                v0, v1 = emu.reg_read(UC_ARM64_REG_V0), emu.reg_read(UC_ARM64_REG_V1)
                a = struct.unpack("<f", (v0 & 0xFFFFFFFF).to_bytes(4, "little"))[0]
                b = struct.unpack("<f", (v1 & 0xFFFFFFFF).to_bytes(4, "little"))[0]
                out = int.from_bytes(struct.pack("<f", math.fmod(a, b)), "little")
                emu.reg_write(UC_ARM64_REG_V0, (v0 & ~0xFFFFFFFF) | out)
                ret()
            elif address == fmod:
                v0, v1 = emu.reg_read(UC_ARM64_REG_V0), emu.reg_read(UC_ARM64_REG_V1)
                a = struct.unpack("<d", (v0 & (2**64 - 1)).to_bytes(8, "little"))[0]
                b = struct.unpack("<d", (v1 & (2**64 - 1)).to_bytes(8, "little"))[0]
                out = int.from_bytes(struct.pack("<d", math.fmod(a, b)), "little")
                emu.reg_write(UC_ARM64_REG_V0, (v0 & ~(2**64 - 1)) | out)
                ret()
            elif address == memcpy:
                dst, src, n = (emu.reg_read(r) for r in (UC_ARM64_REG_X0, UC_ARM64_REG_X1, UC_ARM64_REG_X2))
                emu.mem_write(dst, bytes(emu.mem_read(src, n)))
                ret()
            elif address == free:
                ret()

        uc.hook_add(UC_HOOK_CODE, on_code)
        self.uc = uc

    def run(self, params: bytes) -> bytes:
        from unicorn.arm64_const import UC_ARM64_REG_LR, UC_ARM64_REG_SP, UC_ARM64_REG_X0, UC_ARM64_REG_X8

        self.uc.mem_write(self.params, params)
        self.uc.mem_write(self.result, b"\x00" * _RESULT_SIZE)
        self.uc.reg_write(UC_ARM64_REG_SP, self.sp)
        self.uc.reg_write(UC_ARM64_REG_X0, self.params)
        self.uc.reg_write(UC_ARM64_REG_X8, self.result)
        self.uc.reg_write(UC_ARM64_REG_LR, self.stop)
        self.uc.emu_start(self.entry, self.stop, timeout=5_000_000)
        return bytes(self.uc.mem_read(self.result, _RESULT_SIZE))


def _get_engine() -> "_Emulator | None":
    global _engine, _engine_error
    if _engine is not None or _engine_error is not None:
        return _engine
    path = _so_path()
    if not path.is_file():
        _engine_error = f"vendor library not found at {path}"
        return None
    try:
        _engine = _Emulator(path)
    except ImportError as e:
        _engine_error = f"optional dependency missing ({e.name}); pip install 'garmin-insights[scale-engine]'"
    except Exception as e:  # a corrupt/unexpected binary must not take the server down
        _engine_error = f"failed to load vendor library: {e}"
        logger.warning("WLA37 engine unavailable: %s", e)
    return _engine


def engine_status() -> dict[str, Any]:
    with _lock:
        engine = _get_engine()
    return {"available": engine is not None, "path": str(_so_path()), "error": _engine_error}


def compute_wla37(
    weight_kg: float, height_cm: float, age: float | None, sex: str, impedance_raw: list[int]
) -> dict[str, Any] | None:
    """Vendor body composition, or ``None`` when the engine is unavailable.

    ``impedance_raw`` is the ten words straight from the result frame (ohms x10).
    Returns ``{"metrics": <DB columns>, "extras": <everything else>}``.
    """
    if len(impedance_raw) != 10 or not any(impedance_raw) or not height_cm:
        return None
    sex_code = 2 if str(sex).strip().lower() in ("female", "f", "2") else 1
    params = bytearray(0x118)
    struct.pack_into("<d", params, 0x00, float(weight_kg))
    struct.pack_into("<IIII", params, 0x08, int(round(height_cm)), sex_code, int(age or 0), 37)
    ohms = [v / 10.0 for v in impedance_raw]
    for i in range(5):
        struct.pack_into("<d", params, 0x28 + i * 8, ohms[i])
    for i in range(10):
        struct.pack_into("<d", params, 0x50 + i * 8, ohms[i])
    struct.pack_into("<I", params, 0x110, 10)

    with _lock:  # the emulator is a single shared VM
        engine = _get_engine()
        if engine is None:
            return None
        try:
            raw = engine.run(bytes(params))
        except Exception as e:
            logger.warning("WLA37 emulation failed: %s", e)
            return None

    d = lambda off: round(struct.unpack_from("<d", raw, off)[0], 2)  # noqa: E731
    i = lambda off: struct.unpack_from("<i", raw, off)[0]  # noqa: E731
    fat_pct, muscle_pct = d(0x08), d(0x10)
    fat_mass = round(weight_kg * fat_pct / 100.0, 2)
    return {
        "metrics": {
            "bmi": d(0x00),
            "body_fat_pct": fat_pct,
            "body_water_pct": d(0x30),
            "muscle_mass_kg": round(weight_kg * muscle_pct / 100.0, 2),
            "bone_mass_kg": d(0x28),
            "visceral_fat": d(0x20),
            "metabolic_age": i(0x4C),
        },
        "extras": {
            "composition_engine": "wla37",
            "fat_mass_kg": fat_mass,
            "fat_free_mass_kg": round(weight_kg - fat_mass, 2),
            "bmr_kcal": i(0x48),
            "muscle_pct": muscle_pct,
            # The vendor field at 0x40 is a percentage (matches the app's
            # "Skeletal Muscle %"), despite older notes calling it kg.
            "skeletal_muscle_pct": d(0x40),
            "protein_pct": d(0x38),
            "subcutaneous_fat_pct": d(0x18),
            "body_score": d(0xF0),
            "segments": {
                seg: {
                    "fat_pct": d(_SEGMENT_OFFSETS[seg]),
                    "fat_mass_kg": d(_SEGMENT_OFFSETS[seg] + 8),
                    "muscle_pct": d(_SEGMENT_OFFSETS[seg] + 16),
                    "muscle_mass_kg": d(_SEGMENT_OFFSETS[seg] + 24),
                }
                for seg in _SEGMENTS
            },
        },
    }
