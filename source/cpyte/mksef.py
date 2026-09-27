"""
Scorpion SEF format builder — ELF to SEF conversion.

NOTE: This is a DEVELOPMENT CONVENIENCE TOOL and NOT the recommended route
for production use. It relies on objdump/readelf/objcopy to extract section
data and does not preserve ELF metadata beyond segment contents. For
production, author SEF binaries directly using the format spec in
docs/exec-format.md.
"""

import os
import struct
import subprocess
import sys


def build(elf_path, sef_output, flags=0):
    if not os.path.isfile(elf_path):
        print(f"error: {elf_path}: not found", file=sys.stderr)
        sys.exit(1)

    sections = {}
    result = subprocess.run(
        ["riscv64-elf-objdump", "-h", elf_path], capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"error: objdump failed on {elf_path}", file=sys.stderr)
        sys.exit(1)

    for line in result.stdout.split("\n"):
        parts = line.split()
        if len(parts) >= 4 and parts[0].isdigit():
            name = parts[1]
            if name in (".text", ".rodata", ".data", ".bss"):
                sections[name] = int(parts[2], 16)

    result = subprocess.run(
        ["riscv64-elf-readelf", "-h", elf_path], capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"error: readelf failed on {elf_path}", file=sys.stderr)
        sys.exit(1)

    entry = 0
    for line in result.stdout.split("\n"):
        if "Entry point address" in line:
            entry = int(line.split(":")[1].strip(), 16)

    bin_path = elf_path + ".bin"
    result = subprocess.run(
        ["riscv64-elf-objcopy", "-O", "binary", elf_path, bin_path],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"error: objcopy failed on {elf_path}", file=sys.stderr)
        sys.exit(1)

    with open(bin_path, "rb") as f:
        flat = f.read()
    os.unlink(bin_path)

    segments = []
    off = 0
    if ".text" in sections:
        segments.append((0, off, sections[".text"]))
        off += sections[".text"]
    if ".rodata" in sections:
        segments.append((1, off, sections[".rodata"]))
        off += sections[".rodata"]
    if ".data" in sections:
        segments.append((1, off, sections[".data"]))
        off += sections[".data"]
    if ".bss" in sections:
        segments.append((2, off, sections[".bss"]))

    if not segments:
        print(f"error: {elf_path}: no known sections found", file=sys.stderr)
        sys.exit(1)

    num = len(segments)
    hdr = 12 + num * 16

    out = bytearray()
    out += struct.pack("<IIHH", 0x00464553, entry, num, flags)
    dc = hdr
    for st, sv, ss in segments:
        out += struct.pack("<IIII", st, sv, ss, dc)
        dc += ss
    out += flat

    with open(sef_output, "wb") as f:
        f.write(out)

    print(
        f"Created {sef_output}: {len(out)} bytes, {num} segments, "
        f"entry=0x{entry:x}, flags=0x{flags:x}"
    )


def build_h(sef_path, h_output):
    if not os.path.isfile(sef_path):
        print(f"error: {sef_path}: not found", file=sys.stderr)
        sys.exit(1)

    with open(sef_path, "rb") as f:
        data = f.read()
    base = os.path.splitext(os.path.basename(sef_path))[0]
    guard = f"SCORPION_{base.upper()}_SEF_H"
    define = f"{base.upper()}_SEF_SIZE"
    var = f"{base}_sef"
    with open(h_output, "w") as f:
        f.write(f"#ifndef {guard}\n#define {guard}\n\n")
        f.write(f"#define {define} {len(data)}\n\n")
        f.write(f"static const unsigned char {var}[] = {{\n")
        for i in range(0, len(data), 12):
            chunk = data[i : i + 12]
            f.write("    " + ", ".join(f"0x{b:02x}" for b in chunk) + ",\n")
        f.write("};\n\n#endif\n")
    print(f"Generated {h_output} from {sef_path}")


if __name__ == "__main__":
    args = sys.argv[1:]
    if len(args) < 2:
        print(
            f"usage: {sys.argv[0]} <input.elf> <output.sef>",
            file=sys.stderr,
        )
        sys.exit(1)
    elf_path, sef_output = args[0], args[1]
    # No --flags: privilege is not encoded in the image. Whether an image runs
    # as the manager is decided by the kernel boot path that spawns it.
    build(elf_path, sef_output, 0)
