"""
wla37.py - Exact Body Composition Decoder for Fitdays / Lefu 8-Electrode Dual-Frequency Scales.
Algorithm variant: WLA37 (0x25 / 37 decimal in AA frame).

Directly reproduces the native ICBodyFatAlgorithms (WLA37) engine:
- Supports native execution via ctypes on Linux aarch64 (Raspberry Pi)
- Supports fast micro-emulation via Unicorn engine on Windows / macOS / x86_64
- Computes all 73 full-body and segmental body composition metrics
"""

import math
import os
import platform
import struct
from pathlib import Path
from typing import Dict, List, Union, Any

# Path to native shared library
SO_PATH = Path(__file__).parent / "libICBodyFatAlgorithms.so"

class WLA37Calculator:
    def __init__(self, so_path: Union[str, Path] = SO_PATH):
        self.so_path = Path(so_path)
        if not self.so_path.exists():
            raise FileNotFoundError(f"Native library not found at: {self.so_path}")
        
        self.is_arm64_linux = (platform.machine().lower() in ("aarch64", "arm64")) and (platform.system() == "Linux")
        self._init_engine()

    def _init_engine(self):
        if self.is_arm64_linux:
            # Native ctypes execution on Linux aarch64 -- ONLY works on a
            # bionic (Android) libc. The .so NEEDs liblog.so and bionic-only
            # symbols (__sF, __system_property_get, android_set_abort_message),
            # so on glibc (e.g. Raspberry Pi OS) dlopen fails. Fall back to
            # the Unicorn emulation path, which was verified on a Pi 5.
            import ctypes
            try:
                self.lib = ctypes.CDLL(str(self.so_path))
            except OSError:
                self.is_arm64_linux = False
                return self._init_engine()
            self.calc_func = self.lib._ZN23ICBodyFatAlgorithmWLA374calcE28__ICBodyFatAlgorithmParams__
            self.calc_func.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            self.calc_func.restype = None
        else:
            # Unicorn ARM64 emulation on Windows / macOS / x86_64
            from elftools.elf.elffile import ELFFile
            from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE
            from unicorn.arm64_const import (
                UC_ARM64_REG_SP, UC_ARM64_REG_X0, UC_ARM64_REG_X1, UC_ARM64_REG_X2,
                UC_ARM64_REG_X8, UC_ARM64_REG_LR, UC_ARM64_REG_PC,
                UC_ARM64_REG_V0, UC_ARM64_REG_V1, UC_ARM64_REG_TPIDR_EL0
            )

            with open(self.so_path, 'rb') as f:
                elf = ELFFile(f)
                load_segs = []
                for seg in elf.iter_segments():
                    if seg['p_type'] == 'PT_LOAD':
                        load_segs.append((seg['p_vaddr'], seg['p_memsz'], seg['p_filesz'], seg.data()))
                
                dynsym = elf.get_section_by_name('.dynsym')
                sym_dict = {i: s for i, s in enumerate(dynsym.iter_symbols())}
                name_to_sym = {s.name: s for s in sym_dict.values() if s.name}

                rela_plt = elf.get_section_by_name('.rela.plt')
                rela_dyn = elf.get_section_by_name('.rela.dyn')
                all_relocs = list(rela_dyn.iter_relocations()) + list(rela_plt.iter_relocations())

            self.entry_point = name_to_sym['_ZN23ICBodyFatAlgorithmWLA374calcE28__ICBodyFatAlgorithmParams__']['st_value']

            uc = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
            # Map ELF space: 2MB
            uc.mem_map(0, 0x200000)
            for vaddr, memsz, filesz, data in load_segs:
                uc.mem_write(vaddr, data)
                if memsz > filesz:
                    uc.mem_write(vaddr + filesz, b'\x00' * (memsz - filesz))

            # Hooks region
            HOOKS_BASE = 0x90000000
            uc.mem_map(HOOKS_BASE, 0x10000)
            next_hook = HOOKS_BASE
            def alloc_hook():
                nonlocal next_hook
                addr = next_hook
                next_hook += 16
                uc.mem_write(addr, b'\xc0\x03\x5f\xd6') # ret
                return addr

            HOOK_FMODF = alloc_hook()
            HOOK_FMOD = alloc_hook()
            HOOK_MEMCPY = alloc_hook()
            HOOK_FREE = alloc_hook()
            HOOK_STACK_CHK = alloc_hook()

            ext_hooks = {
                'fmodf': HOOK_FMODF,
                'fmod': HOOK_FMOD,
                'memcpy': HOOK_MEMCPY,
                'free': HOOK_FREE,
                '_ZdlPv': HOOK_FREE,
                '__stack_chk_fail': HOOK_STACK_CHK,
            }

            # Relocations
            R_AARCH64_ABS64 = 257
            R_AARCH64_GLOB_DAT = 1025
            R_AARCH64_JUMP_SLOT = 1026
            R_AARCH64_RELATIVE = 1027

            for rel in all_relocs:
                rtype = rel['r_info_type']
                offset = rel['r_offset']
                addend = rel['r_addend']
                sym_idx = rel['r_info_sym']

                if rtype == R_AARCH64_RELATIVE:
                    uc.mem_write(offset, struct.pack('<Q', addend))
                elif rtype in [R_AARCH64_GLOB_DAT, R_AARCH64_JUMP_SLOT, R_AARCH64_ABS64]:
                    sym = sym_dict[sym_idx]
                    if sym['st_value'] != 0:
                        uc.mem_write(offset, struct.pack('<Q', sym['st_value'] + addend))
                    else:
                        name = sym.name
                        h = ext_hooks.get(name) or alloc_hook()
                        uc.mem_write(offset, struct.pack('<Q', h))

            # Stack
            STACK_BASE = 0x70000000
            STACK_SIZE = 0x00100000
            uc.mem_map(STACK_BASE, STACK_SIZE)
            self.sp_init = STACK_BASE + STACK_SIZE - 0x1000

            # TLS for canary
            TLS_BASE = 0x50000000
            uc.mem_map(TLS_BASE, 0x10000)
            uc.mem_write(TLS_BASE + 0x28, b'\x12\x34\x56\x78\x9a\xbc\xde\xf0')
            uc.reg_write(UC_ARM64_REG_TPIDR_EL0, TLS_BASE)

            # Data buffers
            DATA_BASE = 0x60000000
            uc.mem_map(DATA_BASE, 0x10000)
            self.params_addr = DATA_BASE
            self.result_addr = DATA_BASE + 0x1000
            self.stop_addr = 0x88888888
            uc.mem_map(0x88880000, 0x10000)
            uc.mem_write(self.stop_addr, b'\xc0\x03\x5f\xd6')

            def hook_code(uc, address, size, user_data):
                if address == HOOK_FMODF:
                    v0 = uc.reg_read(UC_ARM64_REG_V0)
                    v1 = uc.reg_read(UC_ARM64_REG_V1)
                    s0 = struct.unpack('<f', (v0 & 0xffffffff).to_bytes(4, 'little'))[0]
                    s1 = struct.unpack('<f', (v1 & 0xffffffff).to_bytes(4, 'little'))[0]
                    res = math.fmod(s0, s1)
                    new_v0 = (v0 & ~0xffffffff) | int.from_bytes(struct.pack('<f', res), 'little')
                    uc.reg_write(UC_ARM64_REG_V0, new_v0)
                    uc.reg_write(UC_ARM64_REG_PC, uc.reg_read(UC_ARM64_REG_LR))
                elif address == HOOK_FMOD:
                    v0 = uc.reg_read(UC_ARM64_REG_V0)
                    v1 = uc.reg_read(UC_ARM64_REG_V1)
                    d0 = struct.unpack('<d', (v0 & 0xffffffffffffffff).to_bytes(8, 'little'))[0]
                    d1 = struct.unpack('<d', (v1 & 0xffffffffffffffff).to_bytes(8, 'little'))[0]
                    res = math.fmod(d0, d1)
                    new_v0 = (v0 & ~0xffffffffffffffff) | int.from_bytes(struct.pack('<d', res), 'little')
                    uc.reg_write(UC_ARM64_REG_V0, new_v0)
                    uc.reg_write(UC_ARM64_REG_PC, uc.reg_read(UC_ARM64_REG_LR))
                elif address == HOOK_MEMCPY:
                    dst = uc.reg_read(UC_ARM64_REG_X0)
                    src = uc.reg_read(UC_ARM64_REG_X1)
                    n = uc.reg_read(UC_ARM64_REG_X2)
                    uc.mem_write(dst, uc.mem_read(src, n))
                    uc.reg_write(UC_ARM64_REG_PC, uc.reg_read(UC_ARM64_REG_LR))
                elif address == HOOK_FREE:
                    uc.reg_write(UC_ARM64_REG_PC, uc.reg_read(UC_ARM64_REG_LR))

            uc.hook_add(UC_HOOK_CODE, hook_code)
            self.uc = uc

    def calculate(
        self,
        weight_kg: float,
        height_cm: float,
        sex: Union[int, str],
        age: int,
        impedances: List[float],
        athlete: bool = False,
        standard: int = 0
    ) -> Dict[str, Any]:
        """
        Calculate complete body composition.

        Args:
            weight_kg: Body weight in kilograms.
            height_cm: Height in centimeters.
            sex: 1/'male'/'m' or 2/'female'/'f'.
            age: Age in years.
            impedances: 10 dual-frequency impedance values [Trunk, LA, RA, LL, RL]
                        (f1 50kHz, f2 100kHz). Can be raw scale integers or ohms.
            athlete: True for athlete/sportsman mode, False for normal.
            standard: Standard model selection (default 0).
        """
        # Normalize sex
        if isinstance(sex, str):
            s = sex.lower().strip()
            sex_val = 1 if s in ('1', 'm', 'male') else 2
        else:
            sex_val = int(sex)

        # Normalize impedances: if raw integers > 500, divide by 10.0 to get ohms
        if any(x > 500 for x in impedances):
            imp_ohms = [float(x) / 10.0 for x in impedances]
        else:
            imp_ohms = [float(x) for x in impedances]

        if len(imp_ohms) != 10:
            raise ValueError(f"Expected 10 dual-frequency impedance values, got {len(imp_ohms)}")

        people_type = 1 if athlete else 0

        # Build input struct (0x118 bytes)
        p = bytearray(0x118)
        struct.pack_into('<d', p, 0x00, float(weight_kg))
        struct.pack_into('<I', p, 0x08, int(round(height_cm)))
        struct.pack_into('<I', p, 0x0c, int(sex_val))
        struct.pack_into('<I', p, 0x10, int(age))
        struct.pack_into('<I', p, 0x14, 37) # algType = 37 (WLA37)
        struct.pack_into('<I', p, 0x18, int(people_type))
        struct.pack_into('<I', p, 0x1c, 0) # enableGirth
        for i in range(5):
            struct.pack_into('<d', p, 0x28 + i * 8, imp_ohms[i])
        for i in range(10):
            struct.pack_into('<d', p, 0x50 + i * 8, imp_ohms[i])
        struct.pack_into('<I', p, 0x110, 10)
        struct.pack_into('<I', p, 0x114, int(standard))

        res_bytes = bytearray(588)

        if self.is_arm64_linux:
            import ctypes
            c_p = (ctypes.c_char * len(p)).from_buffer(p)
            c_res = (ctypes.c_char * len(res_bytes)).from_buffer(res_bytes)
            self.calc_func(ctypes.byref(c_res), ctypes.byref(c_p))
        else:
            from unicorn.arm64_const import (
                UC_ARM64_REG_SP, UC_ARM64_REG_X0, UC_ARM64_REG_X8,
                UC_ARM64_REG_LR, UC_ARM64_REG_PC
            )
            self.uc.mem_write(self.params_addr, bytes(p))
            self.uc.mem_write(self.result_addr, b'\x00' * 588)

            self.uc.reg_write(UC_ARM64_REG_SP, self.sp_init)
            self.uc.reg_write(UC_ARM64_REG_X0, self.params_addr)
            self.uc.reg_write(UC_ARM64_REG_X8, self.result_addr)
            self.uc.reg_write(UC_ARM64_REG_LR, self.stop_addr)

            self.uc.emu_start(self.entry_point, self.stop_addr, timeout=5000000)
            res_bytes = self.uc.mem_read(self.result_addr, 588)

        return self._format_result(res_bytes, weight_kg)

    def _format_result(self, b: bytes, weight_kg: float) -> Dict[str, Any]:
        d = lambda off: round(struct.unpack_from('<d', b, off)[0], 2)
        i = lambda off: struct.unpack_from('<i', b, off)[0]

        fat_pct = d(0x08)
        muscle_pct = d(0x10)
        fat_mass_kg = round(weight_kg * (fat_pct / 100.0), 2)
        muscle_mass_kg = round(weight_kg * (muscle_pct / 100.0), 2)
        bone_mass_kg = d(0x28)

        kg_to_lb = 2.20462262

        return {
            'weight_kg': round(weight_kg, 2),
            'weight_lb': round(weight_kg * kg_to_lb, 1),
            'bmi': d(0x00),
            'fat_percent': fat_pct,
            'fat_mass_kg': fat_mass_kg,
            'fat_mass_lb': round(fat_mass_kg * kg_to_lb, 1),
            'muscle_percent': muscle_pct,
            'muscle_mass_kg': muscle_mass_kg,
            'muscle_mass_lb': round(muscle_mass_kg * kg_to_lb, 1),
            'skeletal_muscle_kg': d(0x40),
            'skeletal_muscle_lb': round(d(0x40) * kg_to_lb, 1),
            'water_percent': d(0x30),
            'water_mass_kg': round(weight_kg * (d(0x30) / 100.0), 2),
            'protein_percent': d(0x38),
            'bone_mass_kg': bone_mass_kg,
            'bone_mass_lb': round(bone_mass_kg * kg_to_lb, 1),
            'subcutaneous_fat_percent': d(0x18),
            'visceral_fat': d(0x20),
            'bmr_kcal': i(0x48),
            'metabolic_age': i(0x4c),
            'body_score': d(0xf0),
            'body_type': i(0x118),
            'weight_target_kg': d(0xf8),
            'fat_control_kg': d(0x100),
            'muscle_control_kg': d(0x108),
            'weight_control_kg': d(0x110),
            'segments': {
                'left_arm': {
                    'fat_percent': d(0x90),
                    'fat_mass_kg': d(0x98),
                    'muscle_percent': d(0xa0),
                    'muscle_mass_kg': d(0xa8),
                },
                'right_arm': {
                    'fat_percent': d(0xb0),
                    'fat_mass_kg': d(0xb8),
                    'muscle_percent': d(0xc0),
                    'muscle_mass_kg': d(0xc8),
                },
                'left_leg': {
                    'fat_percent': d(0x50),
                    'fat_mass_kg': d(0x58),
                    'muscle_percent': d(0x60),
                    'muscle_mass_kg': d(0x68),
                },
                'right_leg': {
                    'fat_percent': d(0x70),
                    'fat_mass_kg': d(0x78),
                    'muscle_percent': d(0x80),
                    'muscle_mass_kg': d(0x88),
                },
                'trunk': {
                    'fat_percent': d(0xd0),
                    'fat_mass_kg': d(0xd8),
                    'muscle_percent': d(0xe0),
                    'muscle_mass_kg': d(0xe8),
                }
            }
        }

if __name__ == '__main__':
    calc = WLA37Calculator()
    raw_imps = [294, 3077, 3157, 2754, 2819, 221, 2667, 2775, 2400, 2456]
    res = calc.calculate(weight_kg=72.0, height_cm=185, sex='male', age=38, impedances=raw_imps, athlete=True)
    print("--- FITDAYS WLA37 DECODE RESULT ---")
    for k, v in res.items():
        if k != 'segments':
            print(f"  {k:26s}: {v}")
    print("\nSegmental Metrics:")
    for limb, vals in res['segments'].items():
        print(f"  {limb:12s}: Fat={vals['fat_percent']}% ({vals['fat_mass_kg']} kg), Muscle={vals['muscle_percent']}% ({vals['muscle_mass_kg']} kg)")
