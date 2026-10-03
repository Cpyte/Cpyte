#!/usr/bin/env python3
"""Regression test for cpyte's Scorpion dynamic-linking toolchain surface.

`--scorpion --pic` links a -fPIC module with --emit-relocs and converts the ELF
to a SEF v2 image carrying SEG_RELOC / SEG_IMPORT / SEG_EXPORT. A library build
also emits, next to the `.sef`:

  * `lib.scorpion.h`  - C ABI header for the exported symbols,
  * `lib.scorpion.cpy` - a cpyte-consumable stub (a `ccode:` block of the same
    prototypes), because SEF export records carry an address and a version hash
    but *no type information* -- the types only exist in the producer module.

That stub is what makes `import "lib.scorpion.cpy"` work in a consumer program,
and it is what gives the call sites something to type-check against. This test
covers

  1. the CLI forwarding of every elf2sef dynamic option
     (--require/--scope/--weak/--lazy/--no-lazy/--versym/--no-versym and the
     `SYMBOL=WIRE_NAME` export form),
  2. the generated header/stub contents, including that the stub names *wire*
     names rather than raw `--export` arguments,
  3. an end-to-end library -> consumer round trip producing SEG_EXPORT and
     SEG_IMPORT records that agree on symbol names,
  4. the guards: `--no-versym` with `--lazy` is rejected, and the dynamic-only
     options warn when `--pic` is absent.

The RISC-V cross toolchain is required; the test skips cleanly when it is
missing so it stays CI-safe on machines without it.

Run standalone:  python test/test_scorpion_dl.py
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FAILURES = []
SKIPPED = []


def check(cond, label, detail=""):
    if cond:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}" + (f"  -- {detail}" if detail else ""))
        FAILURES.append(label)


def skip(label, why):
    print(f"  skip {label}  -- {why}")
    SKIPPED.append(label)


def run(args, cwd=None):
    """Run the cpyte CLI; return (rc, combined output)."""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.join(ROOT, "source")
    p = subprocess.run(
        [sys.executable, "-m", "cpyte"] + args,
        capture_output=True,
        text=True,
        env=env,
        cwd=cwd or ROOT,
    )
    return p.returncode, p.stdout + p.stderr


def have_toolchain():
    """The RISC-V toolchain cpyte needs for --scorpion."""
    sys.path.insert(0, os.path.join(ROOT, "source"))
    try:
        from cpyte import compiling
    except Exception as exc:  # pragma: no cover - import guard
        return False, f"cannot import cpyte.compiling: {exc}"
    for probe in ("gcc", "ld"):
        if compiling._find_scorpion_tool(probe, "") is None:
            return False, f"riscv32-elf {probe} not found"
    if not os.path.isfile(compiling._scorpion_tool("elf2sef.py")):
        return False, "elf2sef.py not available"
    return True, ""


LIB_SRC = """\
public def addup(a: int, b: int) -> int:
    return a + b

public def scale(a: int, k: int) -> int:
    return a * k

public def greet(who: str) -> str:
    return "hi " + who
"""

APP_SRC = """\
import "%(stub)s"

def main() -> int:
    print(addup(2, 3))
    print(scale(4, 5))
    print(greet("cpyte"))
    return 0
"""


def dump(path):
    rc, out = run(["sef", "dump", path])
    return out if rc == 0 else ""


def test_library_exports(tmp):
    lib = os.path.join(tmp, "lib1.cpy")
    with open(lib, "w") as f:
        f.write(LIB_SRC)
    rc, out = run(["--scorpion", "--pic", "--scope", "math", lib])
    check(rc == 0 and "Wrote" in out, "pic library build succeeds", out[-400:])
    if rc != 0:
        return None
    d = dump(os.path.join(tmp, "lib1.sef"))
    check("EXPORT" in d, "library has an EXPORT segment", d[:400])
    check("dynamic=True" in d, "library is marked dynamic", d[:400])
    # --scope prefixes every exported name.
    check("math::addup" in d, "--scope prefixes exports with math::", d[:400])
    return os.path.join(tmp, "lib1.scorpion.cpy")


def test_generated_files(tmp, stub):
    check(
        stub is not None and os.path.isfile(stub), "generated .scorpion.cpy stub exists"
    )
    if not stub or not os.path.isfile(stub):
        return
    src = open(stub).read()
    check("ccode:" in src, "stub carries a ccode: block")
    # The prototypes use fixed-width stdint spellings, and a ccode block is
    # compiled verbatim, so the stub must include the header itself. The riscv
    # clang provides stdint implicitly; a host JIT/AOT build does not, and
    # without the include the stub fails with "unknown type name 'int32_t'".
    check(
        "#include <stdint.h>" in src,
        "stub includes <stdint.h> for its fixed-width prototypes",
    )
    check(
        "#include <stdbool.h>" in src,
        "stub includes <stdbool.h> for its bool prototypes",
    )
    # Prototypes derive from the producer's FuncDefs.
    check(
        re.search(r"int32_t addup\(int32_t a, int32_t b\);", src),
        "stub has a typed addup prototype",
        src[:400],
    )
    check("greet" in src, "stub declares greet")
    # Every ccode line must be indented: a column-0 line ends the block and the
    # following `;` then fails to lex.
    bad = [
        ln
        for ln in src.splitlines()
        if ln.strip() and not ln.startswith((" ", "#")) and ln != "ccode:"
    ]
    check(not bad, "no column-0 line inside the ccode block", repr(bad[:2]))

    hdr = stub.replace(".scorpion.cpy", ".scorpion.h")
    check(os.path.isfile(hdr), "generated .scorpion.h header exists")
    if os.path.isfile(hdr):
        h = open(hdr).read()
        # The runtime lookup is scorpion_sym(); there is no scorpion_dlsym().
        check("scorpion_sym" in h, "header documents scorpion_sym()")
        check(
            "scorpion_dlsym" not in h,
            "header does not reference the nonexistent scorpion_dlsym()",
            h[:300],
        )


def test_wire_names(tmp):
    """`SYMBOL=WIRE_NAME` publishes under the wire name only."""
    lib = os.path.join(tmp, "wire.cpy")
    with open(lib, "w") as f:
        f.write("public def realname(a: int) -> int:\n    return a + 1\n")
    rc, out = run(["--scorpion", "--pic", "--export", "realname=alias1", lib])
    check(rc == 0, "SYMBOL=WIRE_NAME export builds", out[-300:])
    if rc != 0:
        return
    d = dump(os.path.join(tmp, "wire.sef"))
    check("alias1" in d, "wire name is published")
    stub = os.path.join(tmp, "wire.scorpion.cpy")
    if os.path.isfile(stub):
        s = open(stub).read()
        check("realname=" not in s, "stub never leaks a raw --export argument", s[:400])
    # A scoped/versioned wire name cannot be spelled by a cpyte identifier, so
    # the stub must say so rather than emit a prototype that cannot link.
    rc, out = run(["--scorpion", "--pic", "--export", "realname=realname@2.0", lib])
    if rc == 0 and os.path.isfile(os.path.join(tmp, "wire.scorpion.cpy")):
        s = open(os.path.join(tmp, "wire.scorpion.cpy")).read()
        check(
            "not" in s and "importable" in s,
            "versioned wire name is reported as not importable",
            s[:400],
        )


def test_consumer_roundtrip(tmp, stub):
    if not stub:
        return
    app = os.path.join(tmp, "app.cpy")
    with open(app, "w") as f:
        f.write(APP_SRC % {"stub": stub})
    rc, out = run(["--scorpion", "--pic", app])
    check(rc == 0 and "Wrote" in out, "consumer builds against the stub", out[-400:])
    if rc != 0:
        return
    d = dump(os.path.join(tmp, "app.sef"))
    check("IMPORT" in d, "consumer has an IMPORT segment", d[:400])
    for sym in ("addup", "scale", "greet"):
        check(
            re.search(r"import\s+R_\w+\s+slot=\S+\s+%s\b" % sym, d),
            "consumer imports %s" % sym,
            d[:400],
        )


def test_option_forwarding(tmp, stub):
    if not stub:
        return
    app = os.path.join(tmp, "app.cpy")
    base = os.path.join(tmp, "app.sef")

    # --require pins a version on the matching import record.
    rc, out = run(["--scorpion", "--pic", "--require", "addup=2.0", app])
    check(rc == 0, "--require forwards to elf2sef", out[-300:])
    if rc == 0:
        d = dump(base)
        check(
            re.search(r"import\s+R_\w+\s+slot=\S+\s+addup\s+ver=0x[0-9A-F]+", d),
            "--require attaches a version hash to the import",
            d[:400],
        )

    # --no-versym downgrades the image to v2.0 (drops the `versions` flag).
    rc, out = run(["--scorpion", "--pic", "--no-versym", "--no-lazy", app])
    check(rc == 0, "--no-versym/--no-lazy forward", out[-300:])
    if rc == 0:
        d = dump(base)
        check(
            "versions=False" in d and "dynamic=True" in d,
            "--no-versym produces a v2.0 image",
            d[:300],
        )

    # --lazy / --versym are accepted and keep the v2.1 record set.
    rc, out = run(
        [
            "--scorpion",
            "--pic",
            "--lazy",
            "--versym",
            "--weak",
            "nosuchsym",
            "--scope",
            "s",
            app,
        ]
    )
    check(rc == 0, "--lazy/--versym/--weak/--scope forward", out[-300:])


def test_guards(tmp):
    app = os.path.join(tmp, "app.cpy")
    if not os.path.isfile(app):
        return
    # A v2.0 import record has no plt field, so a lazy site would point into an
    # unresolvable hole; reject the combination up front.
    rc, out = run(["--scorpion", "--pic", "--no-versym", "--lazy", app])
    check(
        rc != 0 and "--no-versym" in out,
        "--no-versym with --lazy is rejected",
        out[-200:],
    )

    # Dynamic-only options without --pic warn instead of silently doing nothing.
    lib = os.path.join(tmp, "plain.cpy")
    with open(lib, "w") as f:
        f.write("def main() -> int:\n    print(1)\n    return 0\n")
    rc, out = run(["--scorpion", "--lazy", "--scope", "foo", lib])
    check("add --pic" in out, "dynamic options without --pic warn", out[-200:])


def main():
    ok, why = have_toolchain()
    print("Scorpion dynamic-linking toolchain test")
    if not ok:
        print(f"  skip all  -- {why}")
        print("\nSKIP (no RISC-V cross toolchain)")
        return 0

    tmp = tempfile.mkdtemp(prefix="cpyte-dl-")
    try:
        print("\n[library export]")
        stub = test_library_exports(tmp)
        print("\n[generated header + stub]")
        test_generated_files(tmp, stub)
        print("\n[wire names]")
        test_wire_names(tmp)
        print("\n[consumer round trip]")
        test_consumer_roundtrip(tmp, stub)
        print("\n[option forwarding]")
        test_option_forwarding(tmp, stub)
        print("\n[guards]")
        test_guards(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if FAILURES:
        print(f"FAILED {len(FAILURES)} check(s):")
        for name in FAILURES:
            print(f"  - {name}")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
