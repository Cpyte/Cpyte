import hashlib
import hmac
import os
from traceback import print_exception
from typing import Any, Optional, Protocol

from llvmlite import binding, ir
from llvmlite.ir import instructions

from .astparse import *
from .clib import _BUILTIN_LIB_HEADERS
from .extension_hooks import (
    CodegenHook,
    CompilerContext,
    HookLoadError,
    HookStage,
    RuntimeHook,
    get_global_hook_registry,
)
from .lexar import TokenType
from .optimizations import (
    _FIB_SMALL_TABLE,
    _const_fp_value,
    _const_int_value,
    _count_loop_iterations,
    _detect_fibonacci_pattern,
    _detect_shift_amount_from_mul,
    _find_loop_invariants,
    _fold_int_binop,
    _is_const_one,
    _is_const_zero,
    _is_power_of_2,
    _is_simple_counted_loop,
    _log2_floor,
    _loop_has_alias_risk,
    _loop_written_names,
    _trunc_to_signed,
)
from .ui import *


class _IRValue(Protocol):
    """Anything that carries an LLVM type."""

    type: Any


_BC_ARRAY_SUFFIX_RE = re.compile(r"^(.*)\[\d*\]$")


def _bc_array_back(t):
    """Element type of `T[]` / `T[N]`, or None if `t` is not an array type."""
    if not isinstance(t, str):
        return None
    m = _BC_ARRAY_SUFFIX_RE.match(t)
    return m.group(1) if m else None


def _bc_array_norm(t):
    """Normalize `T[N]` to `T[]` for lowering; both are `T*`.

    Also collapses multi-dimensional arrays (`T[N][M]` -> `T[][]`) so they
    resolve to nested pointer types instead of falling through to an unknown-type
    error / silent `i32` degradation."""
    if not isinstance(t, str):
        return t
    n = 0
    rest = t
    while True:
        m = _BC_ARRAY_SUFFIX_RE.match(rest)
        if not m or not isinstance(m.group(1), str):
            break
        rest = m.group(1)
        n += 1
    if n == 0:
        return t
    return rest + ("[]" * n)


_i8 = ir.IntType(8)
_i32 = ir.IntType(32)
_i64 = ir.IntType(64)
_i1 = ir.IntType(1)
_double = ir.DoubleType()
_void = ir.VoidType()
_i8ptr = ir.PointerType(_i8)

# Runtime kinds for dynamic variables (mirrors runtime.c)
_DYN_NONE = 0
_DYN_INT = 1
_DYN_INT64 = 2
_DYN_UINT64 = 3
_DYN_CHAR = 4
_DYN_BOOL = 5
_DYN_DOUBLE = 6
_DYN_STR = 7
_DYN_BIG = 8
_DYN_PTR = 9
_DYN_LIST = 10

# Runtime slot-value layout for dynamic dispatch (mirrors runtime.c DynValue)
_DynValue = ir.LiteralStructType([_i32, _i64])
_DynValuePtr = ir.PointerType(_DynValue)

# Runtime dispatch opcodes (mirrors runtime.c DYNOP_*)
_DYNOP_ADD = 0
_DYNOP_SUB = 1
_DYNOP_MUL = 2
_DYNOP_DIV = 3
_DYNOP_MOD = 4
_DYNOP_EQ = 5
_DYNOP_NE = 6
_DYNOP_LT = 7
_DYNOP_LE = 8
_DYNOP_GT = 9
_DYNOP_GE = 10
_DYNOP_NEG = 11
_DYNOP_NOT = 12
_DYNOP_BAND = 13
_DYNOP_BOR = 14
_DYNOP_BXOR = 15
_DYNOP_SHL = 16
_DYNOP_SHR = 17
_DYNOP_FLOOR_DIV = 18

# Registry-based visitor: node type -> emit handler
_EMIT_REGISTRY = {}


def register_emitter(node_type):
    def decorator(func):
        _EMIT_REGISTRY[node_type] = func
        return func

    return decorator


# When IR emission recurses deeper than this, the code generator switches to an
# iterative post-order replay so that arbitrarily deep expressions can never
# blow the Python recursion limit. Each nesting level costs a small constant
# number of interpreter frames, so this stays far below the default recursion
# limit (1000) even combined with statement/visit frames.
_EMIT_DEPTH_LIMIT = 120


class _EHBuilder(ir.IRBuilder):
    def __init__(self, block):
        super().__init__(block)
        self._unwind_to = None

    def call(
        self,
        fn,
        args,
        name="",
        cconv=None,
        tail=False,
        fastmath=(),
        attrs=(),
        arg_attrs=None,
    ):
        if self._unwind_to is not None and not isinstance(fn, ir.InlineAsm):
            cont = self.append_basic_block("invoke.cont")
            ret = self.invoke(
                fn,
                args,
                cont,
                self._unwind_to,
                name=name,
                cconv=cconv,
                fastmath=fastmath,
                attrs=attrs,
                arg_attrs=arg_attrs,
            )
            self.position_at_end(cont)
            return ret
        return super().call(
            fn,
            args,
            name=name,
            cconv=cconv,
            tail=tail,
            fastmath=fastmath,
            attrs=attrs,
            arg_attrs=arg_attrs,
        )


def _emit_children(node) -> list:
    """Child nodes that the pure emitters descend into, in emit order."""
    if isinstance(node, BinOp):
        return [node.left, node.right]
    if isinstance(node, UnaryOp):
        return [node.operand]
    if isinstance(node, Deref):
        return [node.operand]
    if isinstance(node, AddrOf):
        return [node.operand]
    if isinstance(node, Index):
        return [node.obj, node.index]
    if isinstance(node, Attr):
        return [node.obj]
    if isinstance(node, NewExpr):
        return [node.size] if node.size is not None else []
    if isinstance(node, Call):
        children = []
        if isinstance(node.callee, Attr):
            children.append(node.callee.obj)
        children.extend(node.args)
        return children
    if isinstance(node, InlineAsm):
        return [arg_expr for _, arg_expr in node.inputs]
    if isinstance(node, ExprStmt):
        return [node.expr]
    if isinstance(node, CastExpr):
        return [node.expr]
    if isinstance(node, ListLit):
        return list(node.items)
    return []


class LLVM:
    def llvm_type(self, t: str):
        # PERFORMANCE: Cache type resolution to avoid repeated string parsing
        if t in self._llvm_type_cache:
            return self._llvm_type_cache[t]

        t = _bc_array_norm(t)
        if t == "int":
            result = ir.IntType(32)
        elif t == "int64":
            result = ir.IntType(64)
        elif t == "uint64":
            result = ir.IntType(64)
        elif t == "size_t":
            # Pointer-sized unsigned integer (i64 on every 64-bit cpyte target,
            # incl. macOS/Linux arm64 & x86-64 LP64). Keeping it i64 (not the
            # i32 fallthrough) lets `(size_t)ptr`/`(ptr)(size_t)` round-trips
            # preserve the full address instead of truncating to 32 bits.
            result = ir.IntType(64)
        elif t == "bool":
            result = ir.IntType(1)
        elif t in ("float", "double"):
            result = ir.DoubleType()
        elif t == "void":
            result = ir.VoidType()
        elif t == "str":
            result = ir.PointerType(ir.IntType(8))
        elif t == "char":
            result = ir.IntType(8)
        elif t == "void*":
            result = ir.PointerType(ir.IntType(8))
        elif t == "big":
            result = ir.PointerType(ir.IntType(8))
        elif t == "ubig":
            result = ir.PointerType(ir.IntType(8))
        elif t == "dynamic":
            # A runtime-typed value: (kind, data) tag pair. Mirrors DynValue.
            result = _DynValue
        elif t == "decorated":
            # Decorator return type: uses DynValue like dynamic.
            result = _DynValue
        elif t.endswith("[]"):
            base = self.llvm_type(t[:-2])
            result = ir.PointerType(base)
        elif t.endswith("*"):
            base = self.llvm_type(t[:-1])
            result = ir.PointerType(base)
        elif t.endswith("&"):
            base = self.llvm_type(t[:-1])
            result = ir.PointerType(base)
        elif t in self.structs:
            result = self.structs[t]
        elif "<" in t:
            # Handle generic types like Pair<int, string>
            # Check if we have a monomorphized version
            if t in self.structs:
                result = self.structs[t]
            else:
                # Try to generate it now
                resolved = self._resolve_generic_type(t)
                if resolved is not None:
                    result = resolved
                else:
                    # Raising here (instead of silently returning i32) is deliberate:
                    # an unresolvable generic type would otherwise corrupt stack layout
                    # at runtime because the value is lowered to a 32-bit int. A clear,
                    # immediate error beats silent memory corruption.
                    self._codegen_error(
                        f"cannot resolve generic type `{t}`: "
                        f"struct `{t.split('<')[0]}` is not defined or the type-argument "
                        f"count does not match its declaration"
                    )
                    result = None  # Should never reach here
        else:
            self._codegen_error(
                f"unknown type `{t}` has no LLVM lowering; cannot emit code for it "
                f"(previously this silently degraded to a 32-bit int)"
            )
            result = None  # Should never reach here

        self._llvm_type_cache[t] = result
        return result

    @staticmethod
    def _codegen_error(msg: str, node=None):
        """Raise a clear codegen error instead of a bare llvmlite assertion."""
        loc = ""
        if node is not None:
            tok = getattr(node, "_token", None)
            if tok is not None and getattr(tok, "line", None) is not None:
                loc = f" at line {tok.line}:{tok.column}"
        raise RuntimeError(f"codegen error{loc}: {msg}")

    def _check_params_no_void(self, param_tys, param_names, node=None):
        """Check that no function parameter has void type."""
        for ty, name in zip(param_tys, param_names):
            if isinstance(ty, ir.VoidType):
                self._codegen_error(
                    f"parameter `{name}` has type `void`; "
                    f"use a pointer type like `void*` instead",
                    node,
                )

    def _base_type_name(self, t: str) -> str:
        while True:
            m = _BC_ARRAY_SUFFIX_RE.match(t)
            if not m or not isinstance(m.group(1), str):
                break
            t = m.group(1)
        while t.endswith(("*", "&")):
            t = t[:-1]
        if '"struct.' in t:
            start = t.index('"struct.') + len('"struct.')
            end = t.index('"', start)
            t = t[start:end]
        idx = t.find("<")
        if idx != -1:
            t = t[:idx]
        return t

    def _resolve_generic_type(self, type_str: str):
        """Resolve a generic type like Pair<int, string> to a monomorphized LLVM struct."""
        if "<" not in type_str:
            return None
        idx = type_str.index("<")
        base_name = type_str[:idx]
        args_str = type_str[idx + 1 : -1]  # strip < and >
        # Parse type args (handle nested generics)
        args = []
        depth = 0
        current = ""
        for ch in args_str:
            if ch == "<":
                depth += 1
                current += ch
            elif ch == ">":
                depth -= 1
                current += ch
            elif ch == "," and depth == 0:
                args.append(current.strip())
                current = ""
            else:
                current += ch
        if current.strip():
            args.append(current.strip())
        # Look up the AST definition for this generic struct. Generic structs are
        # registered in `_struct_nodes` (see emit_structdef) but are *never*
        # placed in `self.structs` as their raw `Pair` form, so use `_struct_nodes`
        # as the source of truth here. Returning early on a missing base struct
        # would silently fall through to a garbage `i32` type below.
        spec_name = f"{base_name}__{'_'.join(a.replace('<', '_').replace('>', '_').replace(',', '_').replace(' ', '') for a in args)}"
        if spec_name in self.structs:
            return self.structs[spec_name]
        struct_node = getattr(self, "_struct_nodes", {}).get(base_name)
        if struct_node is None or not struct_node.generic_params:
            return None
        if len(struct_node.generic_params) != len(args):
            return None
        # Create type substitution map
        type_map = dict(zip(struct_node.generic_params, args))
        # Generate specialized field types
        field_tys = []
        for f in struct_node.fields:
            concrete_type = type_map.get(f.type_expr, f.type_expr)
            field_tys.append(self.llvm_type(concrete_type))
        # Create the specialized struct in this module's context (deduped by
        # name and serialized into the module IR).
        llvm_struct = self.module.context.get_identified_type(f"struct.{spec_name}")
        llvm_struct.set_body(*field_tys)
        self.structs[spec_name] = llvm_struct
        self.struct_fields[spec_name] = struct_node.fields
        return llvm_struct

    def _is_ir_constant_zero(self, val):
        if isinstance(val, ir.Constant) and val.constant == 0:
            return True
        return bool(
            isinstance(val, ir.Constant)
            and isinstance(val.type, ir.PointerType)
            and val.constant is None
        )

    @staticmethod
    def _norm_signed(v, width):
        mask = (1 << width) - 1
        v &= mask
        if v >= 1 << (width - 1):
            v -= 1 << width
        return v

    @staticmethod
    def _trunc_div(a, b):
        q = abs(a) // abs(b)
        return -q if (a < 0) != (b < 0) else q

    def _hook_context(self, data: dict | None = None) -> CompilerContext:
        """Build a CompilerContext exposing this codegen instance to hooks."""
        ctx = CompilerContext(llvm_module=self.module)
        ctx.data["llvm"] = self
        ctx.data["module"] = self.module
        if getattr(self, "builder", None) is not None:
            ctx.data["builder"] = self.builder
        if data:
            ctx.data.update(data)
        return ctx

    def _emit_int_divmod(self, left, right, is_rem):
        """Signed int division/remainder that stays well-defined in LLVM IR.

        cpy ints wrap, so `INT_MIN // -1` yields INT_MIN and `INT_MIN % -1`
        yields 0. Raw `sdiv`/`srem` on those operands is *poison* in LLVM and
        the optimizer folds it into arbitrary values (opt0 vs opt3 diverge).
        Division/remainder by zero still traps, matching the cpy runtime.
        """
        width = left.type.width if isinstance(left.type, ir.IntType) else 64
        int_min = -(1 << (width - 1))

        if isinstance(right, ir.Constant) and not self._is_ir_constant_zero(right):
            # ---- 2.  Constant-division strength reduction ----
            d_raw = self._norm_signed(right.constant, width)
            if d_raw != 0 and d_raw != 1 and d_raw != -1 and d_raw != int_min:
                is_pow2 = _is_power_of_2(abs(d_raw))
                if is_pow2 and not is_rem:
                    # Power-of-two signed division: ashr(x + adjustment, k)
                    # adjustment = (x >> (width-1)) >> (width - k)  (sign correction)
                    k = _log2_floor(abs(d_raw))
                    if d_raw > 0:
                        # Positive divisor: q = ashr(x + (x >> 31 >> (32-k)), k)
                        # For x >= 0 this is just x >> k; for x < 0 we add the
                        # rounding bias so truncation goes toward zero.
                        if isinstance(left, ir.Constant):
                            l = self._norm_signed(left.constant, width)
                            r = self._norm_signed(right.constant, width)
                            if l == int_min and r == -1:
                                return ir.Constant(left.type, int_min)
                            return ir.Constant(left.type, self._trunc_div(l, r))
                        k_const = ir.Constant(left.type, k)
                        sign_bit = self.builder.ashr(
                            left, ir.Constant(left.type, width - 1)
                        )
                        bias = self.builder.and_(
                            sign_bit, ir.Constant(left.type, (1 << k) - 1)
                        )
                        adj = self.builder.add(left, bias)
                        q = self.builder.ashr(adj, k_const)
                        return q
                    else:
                        # Negative divisor: q = -ashr(x + bias, k)
                        if isinstance(left, ir.Constant):
                            l = self._norm_signed(left.constant, width)
                            return ir.Constant(left.type, self._trunc_div(l, d_raw))
                        k_const = ir.Constant(left.type, k)
                        sign_bit = self.builder.ashr(
                            left, ir.Constant(left.type, width - 1)
                        )
                        bias = self.builder.and_(
                            sign_bit, ir.Constant(left.type, (1 << k) - 1)
                        )
                        adj = self.builder.add(left, bias)
                        q = self.builder.ashr(adj, k_const)
                        neg_q = self.builder.neg(q)
                        return neg_q
                elif is_pow2 and is_rem:
                    # Power-of-two signed remainder: x - (q * d)
                    k = _log2_floor(abs(d_raw))
                    if isinstance(left, ir.Constant):
                        l = self._norm_signed(left.constant, width)
                        return ir.Constant(left.type, l % d_raw)
                    k_const = ir.Constant(left.type, k)
                    sign_bit = self.builder.ashr(
                        left, ir.Constant(left.type, width - 1)
                    )
                    bias = self.builder.and_(
                        sign_bit, ir.Constant(left.type, (1 << k) - 1)
                    )
                    adj = self.builder.add(left, bias)
                    q = self.builder.ashr(adj, k_const)
                    if d_raw < 0:
                        q = self.builder.neg(q)
                    d_val = ir.Constant(left.type, abs(d_raw))
                    prod = self.builder.mul(q, d_val)
                    return self.builder.sub(left, prod)
                else:
                    # Non-power-of-two constant divisor: plain sdiv/srem. The
                    # custom magic-number lowering (`_compute_signed_magic`)
                    # was found to be incorrect for large-magnitude dividends,
                    # so we let LLVM do the strength reduction at O1+ (it
                    # lowers sdiv-by-const to a proper magic multiply) and a
                    # correct __divsi3 libcall at O0.
                    if isinstance(left, ir.Constant):
                        l = self._norm_signed(left.constant, width)
                        r = self._norm_signed(right.constant, width)
                        if l == int_min and r == -1:
                            return ir.Constant(left.type, 0 if is_rem else int_min)
                        if is_rem:
                            q = self._trunc_div(l, r)
                            return ir.Constant(left.type, l - q * r)
                        return ir.Constant(left.type, self._trunc_div(l, r))
                    if is_rem:
                        return self.builder.srem(left, right)
                    return self.builder.sdiv(left, right)

            elif isinstance(left, ir.Constant):
                l = self._norm_signed(left.constant, width)
                r = self._norm_signed(right.constant, width)
                if l == int_min and r == -1:
                    return ir.Constant(left.type, 0 if is_rem else int_min)
                if is_rem:
                    q = self._trunc_div(l, r)
                    return ir.Constant(left.type, l - q * r)
                return ir.Constant(left.type, self._trunc_div(l, r))

        if isinstance(right, ir.Constant) and self._is_ir_constant_zero(right):
            # Constant-zero divisor: the operation always traps. Emit a clean
            # unconditional trap (no suppressed `sdiv x, 0` poison hiding in a
            # dead block) and leave the builder in a fresh dead block so callers
            # can keep emitting; this unreachable value is never used.
            trap_bb = self.builder.append_basic_block("div.trap")
            dead_bb = self.builder.append_basic_block("div.dead")
            self.builder.branch(trap_bb)
            self.builder.position_at_end(trap_bb)
            self.builder.call(self._get_trap_fn(), [])
            self.builder.unreachable()
            self.builder.position_at_end(dead_bb)
            return ir.Constant(left.type, 0)

        is_zero = self.builder.icmp_signed("==", right, ir.Constant(right.type, 0))
        trap_bb = self.builder.append_basic_block("div.trap")
        guard_bb = self.builder.append_basic_block("div.guard")
        self.builder.cbranch(is_zero, trap_bb, guard_bb)
        self.builder.position_at_end(trap_bb)
        self.builder.call(self._get_trap_fn(), [])
        self.builder.unreachable()

        self.builder.position_at_end(guard_bb)
        is_min = self.builder.icmp_signed("==", left, ir.Constant(left.type, int_min))
        is_neg1 = self.builder.icmp_signed("==", right, ir.Constant(right.type, -1))
        overflow = self.builder.and_(is_min, is_neg1)
        overflow_bb = self.builder.append_basic_block("div.overflow")
        normal_bb = self.builder.append_basic_block("div.normal")
        end_bb = self.builder.append_basic_block("div.end")
        self.builder.cbranch(overflow, overflow_bb, normal_bb)

        self.builder.position_at_end(overflow_bb)
        self.builder.branch(end_bb)
        self.builder.position_at_end(normal_bb)
        if is_rem:
            normal_val = self.builder.srem(left, right)
        else:
            normal_val = self.builder.sdiv(left, right)
        self.builder.branch(end_bb)
        self.builder.position_at_end(end_bb)
        phi = self.builder.phi(left.type)
        phi.add_incoming(ir.Constant(left.type, 0 if is_rem else int_min), overflow_bb)
        phi.add_incoming(normal_val, normal_bb)
        return phi

    def _emit_floor_div(self, left, right):
        q = self._emit_int_divmod(left, right, is_rem=False)
        prod = self.builder.mul(q, right)
        r = self.builder.sub(left, prod)

        zero = ir.Constant(left.type, 0)
        r_ne_zero = self.builder.icmp_signed("!=", r, zero)
        left_neg = self.builder.icmp_signed("<", left, zero)
        right_neg = self.builder.icmp_signed("<", right, zero)
        diff_signs = self.builder.xor(left_neg, right_neg)
        needs_fix = self.builder.and_(r_ne_zero, diff_signs)

        one = ir.Constant(left.type, 1)
        fix = self.builder.select(needs_fix, one, zero)
        return self.builder.sub(q, fix)

    def _get_floor_fn(self):
        for f in self.module.functions:
            if f.name == "llvm.floor.f64":
                return f
        fnty = ir.FunctionType(ir.DoubleType(), [ir.DoubleType()])
        fn = ir.Function(self.module, fnty, "llvm.floor.f64")
        return fn

    def _clamp_shift_amount(self, val, bitwidth):
        zero = ir.Constant(val.type, 0)
        max_shift = ir.Constant(val.type, bitwidth - 1)
        lt_zero = self.builder.icmp_signed("<", val, zero)
        gt_max = self.builder.icmp_signed(">", val, max_shift)
        clamped_to_max = self.builder.select(gt_max, max_shift, val)
        return self.builder.select(lt_zero, zero, clamped_to_max)

    def _is_type(self, node, t):
        if getattr(node, "inferred_type", "") == t:
            return True
        if isinstance(node, Variable):
            return self.local_types.get(node.name, "") == t
        return False

    def _is_big(self, node):
        return self._is_type(node, "big")

    def _is_ubig(self, node):
        return self._is_type(node, "ubig")

    def _is_biglike(self, node):
        return self._is_big(node) or self._is_ubig(node)

    def _promote_to_big(self, val, src_t=None):
        if isinstance(val.type, ir.IntType) and val.type.width < 64:
            if src_t in ("uint64", "size_t", "ubig", "uint32", "uint16", "uint8"):
                val = self.builder.zext(val, _i64)
            else:
                val = self.builder.sext(val, _i64)

        if isinstance(val.type, ir.IntType):
            if src_t in ("uint64", "size_t", "ubig"):
                fn = self.functions["bigint_from_uint64"]
            else:
                fn = self.functions["bigint_from_int"]
            return self.builder.call(fn, [val])
        return val

    def _promote_to_ubig(self, val):
        if isinstance(val.type, ir.IntType) and val.type.width < 64:
            val = self.builder.zext(val, _i64)

        if isinstance(val.type, ir.IntType):
            fn = self.functions["bigint_from_uint64"]
            return self.builder.call(fn, [val])
        return val

    def __init__(
        self,
        no_userspace=False,
        enable_extensions=True,
        target_triple=None,
        no_gc=False,
        use_native_eh=False,
        debug_instrument=False,
        debug_nids=None,
        debug_step_ids=None,
    ):
        self.module = ir.Module("main")
        self.no_gc = no_gc
        self.use_native_eh = use_native_eh
        # --- cpdb debug instrumentation (opt-in, off by default) -------------
        # When enabled, the emitter emits one `cpdbd_t_<nid>` tracer per step
        # statement, stores the entering node id into `@cpdbd_node`, and
        # refreshes a per-function debug box (`[K x i8*]` globals storing each
        # local's alloca address) so a native debugger can map a stop back to
        # AST nodes and read variables.  `debug_nids` is a dict id(node)->nid
        # and `debug_step_ids` the ids of step statements, both produced by the
        # cpdb AST model (the debug build helper keeps the exact same parse).
        self.debug_instrument = debug_instrument
        self.debug_nids = debug_nids or {}
        self.debug_step_ids = debug_step_ids or set()
        self._dbg_node_global = None
        self._dbg_tracer_fns = {}
        self._dbg_boxes = {}
        self._dbg_box_slots = {}
        self._dbg_box_types = {}
        self._dbg_cur_fn = ""
        self._dbg_emitted = set()
        self._dbg_collect = {}
        self._target_triple = target_triple
        try:
            import llvmlite.binding as _binding

            if target_triple:
                _binding.initialize_all_targets()
                _target = _binding.Target.from_triple(target_triple)
            else:
                _binding.initialize_native_target()
                if os.name == "nt" and not (
                    os.environ.get("WindowsSdkDir")
                    or os.environ.get("VCToolsInstallDir")
                ):
                    # Windows without an MSVC toolchain: target the MinGW GNU
                    # triple so the module matches how the C runtime and the
                    # AOT object files are produced (see compiling.host_target).
                    _target = _binding.Target.from_triple("x86_64-w64-windows-gnu")
                else:
                    _target = _binding.Target.from_default_triple()
            _tm = _target.create_target_machine()
            self.module.triple = _tm.triple
            self.module.data_layout = str(_tm.target_data)
        except (AttributeError, RuntimeError, FileNotFoundError) as e:
            print_exception(e)
        self.builder: Any = None
        self.functions = {}
        self.global_vars = {}
        # Constant Variable store here.
        self.const_vars = {}
        self.locals = {}
        self.local_types = {}
        self.string_id = 0
        self.string_pool = {}
        self.biglit_pool = {}
        self.ssa_values = {}
        self.ssa_types = {}  # Track types of SSA values
        self.scope_stack = []
        self._deferred: list = []  # statements collected by `defer`, run LIFO on function exit
        self.structs = {}
        self.struct_fields = {}
        self.import_src_files = []
        self.loop_stack = []
        self._malloc_fn = None
        self._free_fn = None
        self._gc_alloc_fn = None
        self._gc_write_barrier_fn = None
        self._sizeof_cache = {}
        self._llvm_type_cache = {}  # PERFORMANCE: Cache type resolution
        self._strlen_fn = None
        self._memcpy_fn = None
        self.no_userspace = no_userspace
        self.enable_extensions = enable_extensions
        self._hook_registry = get_global_hook_registry()

        # Decorator enhancement: special variables for advanced decorator features
        self._decorator_args = None  # Access to function arguments
        self._decorator_func_name = None  # Function name metadata
        self._decorator_params = None  # Parameter types metadata
        self._decorator_skip = False  # Flag to skip original function
        self.generic_instantiations = {}  # name -> [(type_args_tuple, ...)]
        self._struct_nodes = {}  # name -> StructDef AST node (for generic resolution)
        self._emit_depth = 0
        self._emit_memo = {}
        self._in_emit_iterative = False
        self._in_decorator = False
        self._is_decorator_factory = False
        self._func_rettype = None  # semantic return type of the current FuncDef
        self._const_prop: dict = {}
        self._const_prop_f: dict = {}

        if not no_userspace:
            print_ty = ir.FunctionType(ir.VoidType(), [ir.IntType(32)])
            print_fn = ir.Function(self.module, print_ty, "print_int")
            self.functions["print_int"] = print_fn
            print_i64_ty = ir.FunctionType(ir.VoidType(), [ir.IntType(64)])
            print_i64_fn = ir.Function(self.module, print_i64_ty, "print_int64")
            self.functions["print_int64"] = print_i64_fn
            print_u64_ty = ir.FunctionType(ir.VoidType(), [ir.IntType(64)])
            print_u64_fn = ir.Function(self.module, print_u64_ty, "print_uint64")
            self.functions["print_uint64"] = print_u64_fn
            print_f_ty = ir.FunctionType(ir.VoidType(), [ir.DoubleType()])
            print_f_fn = ir.Function(self.module, print_f_ty, "print_double")
            self.functions["print_double"] = print_f_fn
            print_x_ty = ir.FunctionType(ir.VoidType(), [ir.IntType(64)])
            print_x_fn = ir.Function(self.module, print_x_ty, "print_hex")
            self.functions["print_hex"] = print_x_fn
            print_s_ty = ir.FunctionType(ir.VoidType(), [ir.PointerType(ir.IntType(8))])
            print_s_fn = ir.Function(self.module, print_s_ty, "print_str")
            self.functions["print_str"] = print_s_fn
            str_i64_ty = ir.FunctionType(_i8ptr, [ir.IntType(64)])
            str_i64_fn = ir.Function(self.module, str_i64_ty, "str_of_int64")
            self.functions["str_of_int64"] = str_i64_fn
            str_u64_ty = ir.FunctionType(_i8ptr, [ir.IntType(64)])
            str_u64_fn = ir.Function(self.module, str_u64_ty, "str_of_uint64")
            self.functions["str_of_uint64"] = str_u64_fn
            str_p_ty = ir.FunctionType(_i8ptr, [ir.IntType(64)])
            str_p_fn = ir.Function(self.module, str_p_ty, "str_of_ptr")
            self.functions["str_of_ptr"] = str_p_fn
            str_f_ty = ir.FunctionType(_i8ptr, [ir.DoubleType()])
            str_f_fn = ir.Function(self.module, str_f_ty, "str_of_double")
            self.functions["str_of_double"] = str_f_fn
            input_ty = ir.FunctionType(ir.IntType(32), [])
            input_fn = ir.Function(self.module, input_ty, "input_int")
            self.functions["input"] = input_fn
            input_str_ty = ir.FunctionType(_i8ptr, [])
            input_str_fn = ir.Function(self.module, input_str_ty, "input_str")
            self.functions["input_str"] = input_str_fn

        # BigNum runtime functions
        bignum_fns = [
            ("bigint_new", _i8ptr, []),
            ("bigint_free", _void, [_i8ptr]),
            ("bigint_from_int", _i8ptr, [_i64]),
            ("bigint_from_uint64", _i8ptr, [_i64]),
            ("bigint_from_str", _i8ptr, [_i8ptr]),
            ("bigint_input", _i8ptr, []),
            ("bigint_add", _i8ptr, [_i8ptr, _i8ptr]),
            ("bigint_sub", _i8ptr, [_i8ptr, _i8ptr]),
            ("bigint_mul", _i8ptr, [_i8ptr, _i8ptr]),
            ("bigint_div", _i8ptr, [_i8ptr, _i8ptr]),
            ("bigint_floor_div", _i8ptr, [_i8ptr, _i8ptr]),
            ("bigint_mod", _i8ptr, [_i8ptr, _i8ptr]),
            ("bigint_neg", _i8ptr, [_i8ptr]),
            ("bigint_cmp", _i32, [_i8ptr, _i8ptr]),
            ("bigint_print", _void, [_i8ptr]),
            ("bigint_to_str", _i8ptr, [_i8ptr]),
            ("bigint_pow", _i8ptr, [_i8ptr, _i8ptr]),
        ]
        for name, ret, args in bignum_fns:
            fn = ir.Function(self.module, ir.FunctionType(ret, args), name=name)
            self.functions[name] = fn

        # Unsigned big (ubig) runtime. sub is underflow-checked; cmp and the
        # bitwise ops operate over the full magnitude (no sign bit to worry about).
        ubig_fns = [
            ("ubigint_sub", _i8ptr, [_i8ptr, _i8ptr]),
            ("ubigint_cmp", _i32, [_i8ptr, _i8ptr]),
            ("ubigint_and", _i8ptr, [_i8ptr, _i8ptr]),
            ("ubigint_or", _i8ptr, [_i8ptr, _i8ptr]),
            ("ubigint_xor", _i8ptr, [_i8ptr, _i8ptr]),
            ("ubigint_shl", _i8ptr, [_i8ptr, _i8ptr]),
            ("ubigint_shr", _i8ptr, [_i8ptr, _i8ptr]),
        ]
        for name, ret, args in ubig_fns:
            fn = ir.Function(self.module, ir.FunctionType(ret, args), name=name)
            self.functions[name] = fn

        # Dynamic variable runtime (name-keyed tagged values)
        dyn_fns = [
            ("assign", _void, [_i8ptr, _i32, _i64]),
            ("dyn_as", _i64, [_i8ptr, _i32]),
            ("dyn_truthy", _i32, [_i8ptr]),
            ("dyn_print", _void, [_i8ptr]),
            ("dyn_str", _i8ptr, [_i8ptr]),
            # Slot-indexed dynamic values + polymorphic dispatch
            ("dyn_slot_ptr", _DynValuePtr, [_i32]),
            ("dyn_set", _void, [_i32, _i32, _i64]),
            ("dyn_kind", _i32, [_i32]),
            ("dyn_get", _i64, [_i32, _i32]),
            ("dyn_as_v", _i64, [_i32, _i64, _i32]),
            ("dyn_truthy_v", _i32, [_i32, _i64]),
            ("dyn_print_v", _void, [_i32, _i64]),
            ("dyn_str_v", _i8ptr, [_i32, _i64]),
            ("dyn_op", _void, [_DynValuePtr, _i32, _i32, _i64, _i32, _i64]),
            ("dyn_op1", _void, [_DynValuePtr, _i32, _i32, _i64]),
            ("dyn_list_get", _DynValuePtr, [_i64, _i64]),
            ("dyn_list_repeat", _DynValuePtr, [_i64, _i64]),
            ("dyn_list_contains", _i32, [_i64, _i32, _i64]),
        ]
        for name, ret, args in dyn_fns:
            fn = ir.Function(self.module, ir.FunctionType(ret, args), name=name)
            self.functions[name] = fn

        # Array length registry (side table for `new T[n]` iteration) plus the
        # append() growth helper (plain-malloc path; GC builds use gc_array_reserve)
        arr_fns = [
            ("cpyte_array_alloc", _i8ptr, [_i64, _i64]),
            ("cpyte_array_len", _i64, [_i8ptr]),
            ("cpyte_array_unregister", _void, [_i8ptr]),
            ("cpyte_array_reserve", _i8ptr, [_i8ptr, _i64, _i64]),
            ("cpyte_array_set_len", _void, [_i8ptr, _i64]),
        ]
        for name, ret, args in arr_fns:
            fn = ir.Function(self.module, ir.FunctionType(ret, args), name=name)
            self.functions[name] = fn

        self.dyn_types = {}

        # Concurrent tri-color GC runtime functions (ugc-based)
        if not self.no_gc:
            gc_fns = [
                ("gc_init", _void, []),
                ("gc_malloc", _i8ptr, [_i64]),
                ("gc_write_barrier", _void, [_i8ptr, _i8ptr]),
                ("gc_collect", _void, []),
                ("gc_array_reserve", _i8ptr, [_i8ptr, _i64, _i64]),
                ("gc_start_thread", _void, []),
                ("gc_stop_thread", _void, []),
                ("gc_shutdown", _void, []),
            ]
            for name, ret, args in gc_fns:
                fn = ir.Function(self.module, ir.FunctionType(ret, args), name=name)
                self.functions[name] = fn

        # Exception handling globals
        self._exc_type = ir.GlobalVariable(self.module, _i8ptr, "_exc_type")
        self._exc_type.initializer = ir.Constant(_i8ptr, None)  # type: ignore[attr-defined]

        if not use_native_eh:
            self._exc_buf_ptr = ir.GlobalVariable(self.module, _i8ptr, "_exc_buf_ptr")
            self._exc_buf_ptr.initializer = ir.Constant(_i8ptr, None)  # type: ignore[attr-defined]
            setjmp_ty = ir.FunctionType(_i32, [_i8ptr])
            self._setjmp_fn = ir.Function(self.module, setjmp_ty, "setjmp")
            self._setjmp_fn.attributes.add("returns_twice")
            longjmp_ty = ir.FunctionType(_void, [_i8ptr, _i32])
            self._longjmp_fn = ir.Function(self.module, longjmp_ty, "longjmp")
            self._longjmp_fn.attributes.add("noreturn")
            # Define functions so dedup function can check duplications. (Error in examples/test.cpy)
            self.functions["setjmp"] = self._setjmp_fn
            self.functions["longjmp"] = self._longjmp_fn
        else:
            pers_ty = ir.FunctionType(_i32, [], var_arg=True)
            self._personality_fn = ir.Function(self.module, pers_ty, "cpy_personality")
            raise_ty = ir.FunctionType(_void, [_i8ptr, _i8ptr])
            self._raise_exception_fn = ir.Function(
                self.module, raise_ty, "cpy_raise_exception"
            )
            resume_ty = ir.FunctionType(_void, [_i8ptr])
            self._resume_fn = ir.Function(self.module, resume_ty, "cpy_resume")
            # Defined for the same reason
            self.functions["resume"] = self._resume_fn
            self.functions["raise"] = self._raise_exception_fn
            self.functions["personality"] = self._personality_fn

        # strcmp for exception type matching
        strcmp_ty = ir.FunctionType(_i32, [_i8ptr, _i8ptr])
        self._strcmp_fn = None
        if not self.module.scope.is_used("strcmp"):
            self._strcmp_fn = ir.Function(self.module, strcmp_ty, "strcmp")
        else:
            for g in self.module.globals:
                if isinstance(g, ir.Function) and g.name == "strcmp":
                    self._strcmp_fn = g
                    break
        # If strcmp was already used but only exists as a global/alias (not an
        # ir.Function), synthesize a fresh declaration so downstream calls never
        # hit an unbound-name crash.
        if self._strcmp_fn is None:
            self._strcmp_fn = ir.Function(self.module, strcmp_ty, "strcmp")
        self.functions["strcmp"] = self._strcmp_fn

        # Shell-style glob matching for string switch cases (always in runtime.c)
        glob_ty = ir.FunctionType(_i32, [_i8ptr, _i8ptr])
        self.functions["glob_match"] = ir.Function(self.module, glob_ty, "glob_match")

        # Decorator support: code() function pointer + result slot
        self._code_fn_ptr = ir.GlobalVariable(self.module, _i8ptr, name="__code_fn")
        self._code_fn_ptr.initializer = ir.Constant(_i8ptr, None)  # type: ignore[attr-defined]
        self._code_result = ir.GlobalVariable(
            self.module, _DynValue, name="__code_result"
        )
        self._code_result.initializer = ir.Constant(
            _DynValue, (ir.Constant(_i32, _DYN_NONE), ir.Constant(_i64, 0))
        )  # type: ignore[attr-defined]
        # Decorator access: the wrapper boxes the decorated function's incoming
        # args, name and skip flag into these globals so decorator factory bodies
        # (`rettype ... = "decorated"`) can read `args`/`func_name` and write
        # `skip = true` (mirrors the existing __code_fn/__code_result routing).
        self._code_args = ir.GlobalVariable(
            self.module, _DynValuePtr, name="__code_args"
        )
        self._code_args.initializer = ir.Constant(_DynValuePtr, None)  # type: ignore[attr-defined]
        self._code_fn_name = ir.GlobalVariable(
            self.module, _i8ptr, name="__code_fn_name"
        )
        self._code_fn_name.initializer = ir.Constant(_i8ptr, None)  # type: ignore[attr-defined]
        self._code_skip = ir.GlobalVariable(
            self.module, ir.IntType(1), name="__code_skip"
        )
        self._code_skip.initializer = ir.Constant(ir.IntType(1), 0)  # type: ignore[attr-defined]

    # CRITICAL CONSTANT FOLDING BUG
    @register_emitter(Switch)
    def emit_switch(self, node):
        val_ty = getattr(node.value, "inferred_type", None)
        if val_ty == "str":
            return self._emit_switch_glob(node)
        if not self.no_userspace and (
            val_ty == "dynamic" or self._is_dynamic_expr(node.value)
        ):
            return self._emit_switch_dynamic(node)

        value = self.emit(node.value)
        if not isinstance(value.type, ir.IntType):
            value = self._is_true(value)

        end_blk = self.builder.append_basic_block(name="sw_end")
        case_irs = []

        # Step 1: Pre-evaluate all case expressions
        for case_val, _ in node.cases:
            if case_val is None:
                case_irs.append(None)
            else:
                val_ir = self.emit(case_val)
                # Ensure bit-width matching
                if (
                    isinstance(val_ir.type, ir.IntType)
                    and val_ir.type.width != value.type.width
                ):
                    if isinstance(val_ir, ir.Constant):
                        val_ir = ir.Constant(value.type, val_ir.constant)
                    elif val_ir.type.width < value.type.width:
                        val_ir = self.builder.zext(val_ir, value.type)
                    else:
                        val_ir = self.builder.trunc(val_ir, value.type)
                case_irs.append(val_ir)

        # Step 2: Handle Dynamic/Non-Constant Expressions via Branch Cascade
        if any(c is not None and not isinstance(c, ir.Constant) for c in case_irs):
            default_blk = end_blk

            for i, (case_val, body) in enumerate(node.cases):
                if case_val is None:
                    # Collect default block body insertion point
                    default_blk = self.builder.append_basic_block(name="sw_default")
                    curr_pos = self.builder.block
                    self.builder.position_at_end(default_blk)
                    self._push_scope()
                    for stmt in body:
                        self.emit(stmt)
                    self._pop_scope()
                    if not self._block_terminated():
                        self.builder.branch(end_blk)
                    self.builder.position_at_end(curr_pos)
                else:
                    val_ir = case_irs[i]
                    then_blk = self.builder.append_basic_block(name="sw_case")
                    nxt_blk = self.builder.append_basic_block(name="sw_next")

                    eq = self.builder.icmp_signed("==", value, val_ir)
                    self.builder.cbranch(eq, then_blk, nxt_blk)

                    # Populate matching case body
                    self.builder.position_at_end(then_blk)
                    self._push_scope()
                    for stmt in body:
                        self.emit(stmt)
                    self._pop_scope()
                    if not self._block_terminated():
                        self.builder.branch(end_blk)

                    # Move builder to next evaluation block
                    self.builder.position_at_end(nxt_blk)

            # Fall through remaining condition to default/end
            self.builder.branch(default_blk)
            self.builder.position_at_end(end_blk)
            return

        # Step 3: Handle Constant Expressions via Native LLVM Switch Instruction
        case_blks = []
        default_blk = end_blk

        for i, (case_val, _) in enumerate(node.cases):
            if case_val is None:
                default_blk = self.builder.append_basic_block(name="sw_default")
                case_blks.append(default_blk)
            else:
                case_blks.append(self.builder.append_basic_block(name="sw_case"))

        sw = self.builder.switch(value, default_blk)
        for i, (case_val, _) in enumerate(node.cases):
            if case_val is not None:
                sw.add_case(case_irs[i], case_blks[i])

        # Emit code for each case block
        for i, (_, body) in enumerate(node.cases):
            self.builder.position_at_end(case_blks[i])
            self._push_scope()
            for stmt in body:
                self.emit(stmt)
            self._pop_scope()
            if not self._block_terminated():
                self.builder.branch(end_blk)

        self.builder.position_at_end(end_blk)

    def _emit_switch_glob(self, node):
        """String switch: each case value is a shell-style glob pattern."""
        value = self.emit(node.value)
        end_blk = self.builder.append_basic_block(name="sw_end")
        default_bodies = []
        for i, (case_val, body) in enumerate(node.cases):
            if case_val is None:
                default_bodies.append(body)
                continue
            pat = self.emit(case_val)
            res = self.builder.call(self.functions["glob_match"], [pat, value])
            is_match = self.builder.icmp_signed("!=", res, ir.Constant(_i32, 0))
            case_blk = self.builder.append_basic_block(name="sw_case")
            nxt_blk = self.builder.append_basic_block(name="sw_next")
            self.builder.cbranch(is_match, case_blk, nxt_blk)
            self.builder.position_at_start(case_blk)
            self._push_scope()
            for stmt in body:
                self.emit(stmt)
            self._pop_scope()
            if not self._block_terminated():
                self.builder.branch(end_blk)
            self.builder.position_at_start(nxt_blk)
        if default_bodies:
            default_blk = self.builder.append_basic_block(name="sw_default")
            self.builder.branch(default_blk)
            self.builder.position_at_start(default_blk)
            for body in default_bodies:
                self._push_scope()
                for stmt in body:
                    self.emit(stmt)
                self._pop_scope()
                if not self._block_terminated():
                    self.builder.branch(end_blk)
        else:
            self.builder.branch(end_blk)
        self.builder.position_at_start(end_blk)

    def _emit_switch_dynamic(self, node):
        """Dynamic switch: string case patterns use glob matching, other case
        values dispatch through the runtime equality operator."""
        k, b = self._dyn_pair(node.value)
        end_blk = self.builder.append_basic_block(name="sw_end")
        default_bodies = []
        for case_val, body in node.cases:
            if case_val is None:
                default_bodies.append(body)
                continue
            case_ty = getattr(case_val, "inferred_type", None)
            if case_ty == "str":
                pat = self.emit(case_val)
                str_ptr = self.builder.inttoptr(b, _i8ptr)
                is_str = self.builder.icmp_signed("==", k, ir.Constant(_i32, _DYN_STR))
                safe_ptr = self.builder.select(is_str, str_ptr, self._string_const(""))
                res = self.builder.call(self.functions["glob_match"], [pat, safe_ptr])
                glob_hit = self.builder.icmp_signed("!=", res, ir.Constant(_i32, 0))
                is_match = self.builder.and_(is_str, glob_hit)
            else:
                cv = self.emit(case_val)
                if cv.type == _DynValue:
                    ck = self.builder.extract_value(cv, 0)
                    cb = self.builder.extract_value(cv, 1)
                else:
                    ck_i, cb_i = self._box_dyn(cv, case_ty or "int")
                    ck = ir.Constant(_i32, ck_i)
                    cb = cb_i
                out_ptr = self._alloca(_DynValue, name="dyn.swcase")
                self.builder.call(
                    self.functions["dyn_op"],
                    [out_ptr, ir.Constant(_i32, _DYNOP_EQ), k, b, ck, cb],
                )
                out = self.builder.load(out_ptr)
                out_bits = self.builder.extract_value(out, 1)
                is_match = self.builder.trunc(out_bits, _i1)
            case_blk = self.builder.append_basic_block(name="sw_case")
            nxt_blk = self.builder.append_basic_block(name="sw_next")
            self.builder.cbranch(is_match, case_blk, nxt_blk)
            self.builder.position_at_start(case_blk)
            self._push_scope()
            for stmt in body:
                self.emit(stmt)
            self._pop_scope()
            if not self._block_terminated():
                self.builder.branch(end_blk)
            self.builder.position_at_start(nxt_blk)
        if default_bodies:
            default_blk = self.builder.append_basic_block(name="sw_default")
            self.builder.branch(default_blk)
            self.builder.position_at_start(default_blk)
            for body in default_bodies:
                self._push_scope()
                for stmt in body:
                    self.emit(stmt)
                self._pop_scope()
                if not self._block_terminated():
                    self.builder.branch(end_blk)
        else:
            self.builder.branch(end_blk)
        self.builder.position_at_start(end_blk)

    _JMP_BUF_SIZE = 200

    @register_emitter(Try)
    def emit_try(self, node):
        if self.use_native_eh:
            return self._emit_try_native(node)
        return self._emit_try_setjmp(node)

    def _emit_try_setjmp(self, node):
        buf = self._alloca(ir.ArrayType(_i8, self._JMP_BUF_SIZE), "exc_buf")
        buf_ptr = self.builder.gep(buf, [ir.Constant(_i32, 0), ir.Constant(_i32, 0)])
        old_buf = self.builder.load(self._exc_buf_ptr)
        self.builder.store(buf_ptr, self._exc_buf_ptr)
        result = self.builder.call(self._setjmp_fn, [buf_ptr])
        is_exception = self.builder.icmp_signed("!=", result, ir.Constant(_i32, 0))
        try_blk = self.builder.append_basic_block("try.body")
        handler_blk = self.builder.append_basic_block("try.handler")
        after_blk = self.builder.append_basic_block("try.after")
        self.builder.cbranch(is_exception, handler_blk, try_blk)

        self.builder.position_at_end(try_blk)
        for stmt in node.body:
            if not self._block_terminated():
                self.emit(stmt)
        if not self._block_terminated():
            self.builder.store(old_buf, self._exc_buf_ptr)
            self.builder.branch(after_blk)

        self.builder.position_at_end(handler_blk)
        exc_type_val = self.builder.load(self._exc_type)
        last_blk = handler_blk
        for i, handler in enumerate(node.handlers):
            if handler.type_name:
                htype = self._string_const(handler.type_name)
                cmp_res = self.builder.call(self._strcmp_fn, [exc_type_val, htype])
                is_match = self.builder.icmp_signed("==", cmp_res, ir.Constant(_i32, 0))
                match_blk = self.builder.append_basic_block(f"try.match.{i}")
                next_blk = self.builder.append_basic_block(f"try.next.{i}")
                self.builder.cbranch(is_match, match_blk, next_blk)

                self.builder.position_at_end(match_blk)
                self.builder.store(old_buf, self._exc_buf_ptr)
                for stmt in handler.body:
                    if not self._block_terminated():
                        self.emit(stmt)
                if not self._block_terminated():
                    self.builder.branch(after_blk)

                self.builder.position_at_end(next_blk)
            else:
                self.builder.store(old_buf, self._exc_buf_ptr)
                for stmt in handler.body:
                    if not self._block_terminated():
                        self.emit(stmt)
                if not self._block_terminated():
                    self.builder.branch(after_blk)

        if not self._block_terminated():
            self.builder.store(old_buf, self._exc_buf_ptr)
            self.builder.branch(after_blk)

        self.builder.position_at_end(after_blk)

    def _emit_try_native(self, node):
        fn = self.builder.function
        fn.attributes.add("uwtable")
        fn.attributes.personality = self._personality_fn
        if not isinstance(self.builder, _EHBuilder):
            self.builder = _EHBuilder(self.builder.block)

        try_blk = self.builder.append_basic_block("try.body")
        lpad_blk = self.builder.append_basic_block("try.lpad")
        after_blk = self.builder.append_basic_block("try.after")
        self.builder.branch(try_blk)
        self.builder.position_at_end(try_blk)

        saved_unwind = self.builder._unwind_to
        self.builder._unwind_to = lpad_blk
        for stmt in node.body:
            if not self._block_terminated():
                self.emit(stmt)
        self.builder._unwind_to = saved_unwind
        if not self._block_terminated():
            self.builder.branch(after_blk)

        self.builder.position_at_end(lpad_blk)
        lp = self.builder.landingpad(ir.LiteralStructType([_i8ptr, _i32]), name="lp")
        lp.add_clause(instructions.CatchClause(ir.Constant(_i8ptr, None)))
        exc = self.builder.extract_value(lp, 0)
        exc_struct = ir.LiteralStructType([_i64, _i8ptr, _i64, _i64, _i8ptr, _i8ptr])
        es = self.builder.bitcast(exc, ir.PointerType(exc_struct))
        tnp = self.builder.gep(es, [ir.Constant(_i32, 0), ir.Constant(_i32, 4)])
        exc_type_val = self.builder.load(tnp)

        for i, handler in enumerate(node.handlers):
            if handler.type_name:
                htype = self._string_const(handler.type_name)
                cmp_res = self.builder.call(self._strcmp_fn, [exc_type_val, htype])
                is_match = self.builder.icmp_signed("==", cmp_res, ir.Constant(_i32, 0))
                match_blk = self.builder.append_basic_block(f"try.match.{i}")
                next_blk = self.builder.append_basic_block(f"try.next.{i}")
                self.builder.cbranch(is_match, match_blk, next_blk)

                self.builder.position_at_end(match_blk)
                for stmt in handler.body:
                    if not self._block_terminated():
                        self.emit(stmt)
                if not self._block_terminated():
                    self.builder.branch(after_blk)

                self.builder.position_at_end(next_blk)
            else:
                for stmt in handler.body:
                    if not self._block_terminated():
                        self.emit(stmt)
                if not self._block_terminated():
                    self.builder.branch(after_blk)

        if not self._block_terminated():
            self.builder.call(self._resume_fn, [exc])
            self.builder.unreachable()

        self.builder.position_at_end(after_blk)

    @register_emitter(Raise)
    def emit_raise(self, node):
        if self.use_native_eh:
            return self._emit_raise_native(node)
        return self._emit_raise_setjmp(node)

    def _emit_raise_setjmp(self, node):
        exc_type_str = self._string_const(node.exc_type)
        self.builder.store(exc_type_str, self._exc_type)
        buf = self.builder.load(self._exc_buf_ptr)
        self.builder.call(self._longjmp_fn, [buf, ir.Constant(_i32, 1)])
        self.builder.unreachable()

    def _emit_raise_native(self, node):
        exc_type_str = self._string_const(node.exc_type)
        msg = self.emit(node.message)
        self.builder.call(self._raise_exception_fn, [exc_type_str, msg])
        self.builder.unreachable()

    def emit_program(self, ast):
        structs = []
        imports = []
        funcdefs = []
        toplevel = []

        def _collect_nodes(nodes):
            for node in nodes:
                if isinstance(node, (StructDef, ClassDef)):
                    structs.append(node)
                elif isinstance(node, Import):
                    imports.append(node)
                    if getattr(node, "sub_ast", None):
                        _collect_nodes(node.sub_ast)
                elif isinstance(node, FuncDef):
                    funcdefs.append(node)
                else:
                    toplevel.append(node)

        _collect_nodes(ast)

        # Collect runtime code from extension hooks
        if self.enable_extensions:
            import os
            import tempfile

            context = self._hook_context()
            for hook in self._hook_registry.get(HookStage.RUNTIME):
                if not isinstance(hook, RuntimeHook):
                    continue
                try:
                    for rel_file in hook.get_runtime_files(context):
                        base = os.path.dirname(hook.hook_path) if hook.hook_path else ""
                        abs_path = os.path.join(base, rel_file)
                        if os.path.isfile(abs_path):
                            self.import_src_files.append(abs_path)
                    runtime_code = hook.get_runtime_code(context)
                    if runtime_code:
                        fd, tmp_path = tempfile.mkstemp(
                            suffix=".c", prefix="hook_runtime_"
                        )
                        with os.fdopen(fd, "w") as f:
                            f.write(runtime_code)
                        self.import_src_files.append(tmp_path)
                except Exception:
                    pass

        for node in structs:
            if isinstance(node, ClassDef):
                continue
            # Register an opaque identified struct in THIS module's context
            # (not the shared global context) so self-referential fields resolve
            # to a pointer to the same type, and so per-program modules compiled
            # in one process don't collide on struct names.
            st = self.module.context.get_identified_type(f"struct.{node.name}")
            self.structs[node.name] = st

        for node in structs:
            self.emit(node)

        for node in imports:
            self.emit(node)

        # Emit all ccode/llvm blocks before function definitions so that
        # ccode-declared and llvm-declared functions are available to every
        # function body (including public funcs pulled in from imports).
        def _collect_ccodes(nodes):
            for node in nodes:
                if isinstance(node, (CCode, Llvm)):
                    self.emit(node)
                elif isinstance(node, FuncDef):
                    _collect_ccodes(getattr(node, "body", None) or [])

        _collect_ccodes(toplevel)
        _collect_ccodes(funcdefs)

        user_main = None
        for node in funcdefs:
            if getattr(node, "name", None) == "main":
                user_main = node
                break

        if toplevel and user_main is not None:
            user_main.body = toplevel + user_main.body
            toplevel = []

        wrapper_builder = None
        if user_main is None:
            main = ir.Function(self.module, ir.FunctionType(ir.IntType(32), []), "main")
            entry = main.append_basic_block("entry")
            wrapper_builder = ir.IRBuilder(entry)

        for node in funcdefs:
            self.emit(node)

        if wrapper_builder is not None:
            self.builder = wrapper_builder
            if self.debug_instrument:
                self._dbg_begin_function("main", {}, list(toplevel))
            if not self.no_gc:
                # Initialize concurrent tri-color GC and start background thread
                gc_init_fn = self.functions.get("gc_init")
                if gc_init_fn:
                    self.builder.call(gc_init_fn, [])
                gc_start_fn = self.functions.get("gc_start_thread")
                if gc_start_fn:
                    self.builder.call(gc_start_fn, [])
            for node in toplevel:
                self.emit(node)
            if not self.no_gc:
                gc_shutdown_fn = self.functions.get("gc_shutdown")
                if gc_shutdown_fn:
                    self.builder.call(gc_shutdown_fn, [])
            self.builder.ret(ir.Constant(ir.IntType(32), 0))

        return self.module, self.import_src_files

    @register_emitter(FString)
    def emit_fstring(self, node):
        parts = node.parts
        strings = []
        for kind, payload in parts:
            if kind == "lit":
                strings.append(self._string_const(payload))
            elif (
                isinstance(payload, Variable)
                and getattr(payload, "dynamic", False)
                and not self.no_userspace
            ):
                strings.append(
                    self.builder.call(
                        self.functions["dyn_str"], [self._dyn_name(payload.name)]
                    )
                )
            else:
                val = self.emit(payload)  # pyright: ignore[reportArgumentType]
                strings.append(self._stringify_value(payload, val))
        result = strings[0] if strings else self._string_const("")
        for s in strings[1:]:
            result = self._concat_strings(result, s)
        return result

    def _stringify_value(self, node, val):
        """Convert a runtime value into a heap string (i8*) for interpolation."""
        if val.type == _DynValue and not self.no_userspace:
            k = self.builder.extract_value(val, 0)
            b = self.builder.extract_value(val, 1)
            return self.builder.call(self.functions["dyn_str_v"], [k, b])
        if (
            isinstance(node, Variable)
            and getattr(node, "dynamic", False)
            and not self.no_userspace
        ):
            return self.builder.call(
                self.functions["dyn_str"], [self._dyn_name(node.name)]
            )
        if getattr(node, "inferred_type", None) == "str":
            return val
        if isinstance(node, Number) and (
            node.value.startswith("0x") or node.value.startswith("0X")
        ):
            as_i64 = self._extend_to_i64(val)
            return self.builder.call(self.functions["str_of_ptr"], [as_i64])
        if getattr(node, "inferred_type", None) in ("big", "ubig"):
            return self.builder.call(self.functions["bigint_to_str"], [val])
        ty = val.type
        if isinstance(ty, ir.PointerType):
            as_i64 = self.builder.ptrtoint(val, _i64)
            return self.builder.call(self.functions["str_of_ptr"], [as_i64])
        if isinstance(ty, ir.DoubleType):
            return self.builder.call(self.functions["str_of_double"], [val])
        if isinstance(ty, ir.FloatType):
            as_double = self.builder.fpext(val, ir.DoubleType())
            return self.builder.call(self.functions["str_of_double"], [as_double])
        if isinstance(ty, ir.IntType):
            i64 = self._extend_to_i64(val)
            if getattr(node, "inferred_type", None) == "uint64":
                return self.builder.call(self.functions["str_of_uint64"], [i64])
            return self.builder.call(self.functions["str_of_int64"], [i64])
        as_i64 = self.builder.ptrtoint(val, _i64)
        return self.builder.call(self.functions["str_of_ptr"], [as_i64])

    def _concat_strings(self, left, right):
        """Concatenate two heap strings (i8*), returning a new heap string."""
        strlen_fn = self._get_strlen_fn()
        malloc_fn = self._get_malloc_fn()
        memcpy_fn = self._get_memcpy_fn()
        left_len = self.builder.call(strlen_fn, [left])
        right_len = self.builder.call(strlen_fn, [right])
        total_len = self.builder.add(left_len, right_len)
        plus_one = self.builder.add(total_len, ir.Constant(total_len.type, 1))
        new_str = self.builder.call(malloc_fn, [self.builder.zext(plus_one, _i64)])
        self.builder.call(memcpy_fn, [new_str, left, left_len])
        dest_plus = self.builder.gep(new_str, [left_len], inbounds=True)
        self.builder.call(memcpy_fn, [dest_plus, right, right_len])
        null_byte = self.builder.gep(new_str, [total_len], inbounds=True)
        self.builder.store(ir.Constant(_i8, 0), null_byte)
        return new_str

    @register_emitter(ExprStmt)
    def emit_exprstmt(self, node):
        return self.emit(node.expr)

    @register_emitter(DeferStmt)
    def emit_deferstmt(self, node: DeferStmt):
        # Collect the deferred statement; it is emitted (in reverse order) at
        # the function's exit points, both explicit `return` and implicit end.
        self._deferred.append(node.body)

    def emit(self, node: Node | dict) -> _IRValue:
        key = id(node)
        if key in self._emit_memo:
            return self._emit_memo[key]
        if self.debug_instrument and self.builder is not None:
            nid = self.debug_nids.get(key)
            if nid is not None and key in self.debug_step_ids:
                self._dbg_emit_tracer(node, nid)
        if not self._in_emit_iterative:
            self._emit_depth += 1
            try:
                if self._emit_depth > _EMIT_DEPTH_LIMIT:
                    return self._emit_iterative(node)
                return self._emit_recursive(node)
            finally:
                self._emit_depth -= 1
        return self._emit_recursive(node)

    _EMIT_PURE_TYPES = (
        Number,
        String,
        Variable,
        Signed67,
        Input,
        InputStr,
        InputBig,
        SizeOf,
        UnaryOp,
        Deref,
        AddrOf,
        Index,
        Attr,
        NewExpr,
        Call,
        InlineAsm,
        CastExpr,
        ListLit,
        ExprStmt,
    )

    def _emit_is_pure(self, node) -> bool:
        if isinstance(node, BinOp):
            return node.op not in (TokenType.AND, TokenType.OR)
        return isinstance(node, self._EMIT_PURE_TYPES)

    @register_emitter(CCode)
    def emit_ccode(self, node: CCode):
        """Link an embedded `ccode:` block as C and declare its functions.

        The block's C body is written to a temp .c file appended to
        `import_src_files` so both the JIT and AOT paths compile and link it
        alongside the C runtime. Each function the semantic pass registered on
        the node is declared as an extern LLVM function so cpyte call sites
        resolve to it.
        """
        import os
        import tempfile

        if getattr(self, "_ccode_emitted", None) is None:
            self._ccode_emitted = set()
        symbols = getattr(node, "symbols", None)
        # Dedup by declared symbol names + body so a shared helper module (e.g.
        # mem.cpy) imported from many containers emits one temp .c instead of N
        # identical copies that collide at link time.
        key = id(node)
        if node.value:
            names = tuple(sorted(fname for fname, _sig in (symbols or [])))
            key = (names, node.value)
        if key in self._ccode_emitted:
            return
        self._ccode_emitted.add(key)
        if symbols:
            var_names = getattr(node, "var_names", set()) or set()
            for fname, (ret_type, params, vararg) in symbols:
                if fname in self.functions or fname in self.global_vars:
                    continue
                if fname in var_names:
                    var_ty = self.llvm_type(ret_type)
                    if isinstance(var_ty, ir.VoidType):
                        continue
                    gv = ir.GlobalVariable(self.module, var_ty, name=fname)
                    gv.linkage = "extern_weak"
                    self.global_vars[fname] = gv
                else:
                    ret_ty = self.llvm_type(ret_type)
                    if isinstance(ret_ty, ir.VoidType) and not params and not vararg:
                        param_tys = []
                    else:
                        param_tys = [self.llvm_type(t) for _, t in params]
                    self._check_params_no_void(param_tys, [n for n, _ in params])
                    fnty = ir.FunctionType(ret_ty, param_tys, var_arg=vararg)
                    func = ir.Function(self.module, fnty, name=fname)
                    self.functions[fname] = func
        if node.value:
            fd, tmp_path = tempfile.mkstemp(suffix=".c", prefix="ccode_")
            includes = getattr(node, "includes", None) or []
            if includes:
                header_lines = "".join(
                    f"#include <{_BUILTIN_LIB_HEADERS.get(lib, lib)}>\n"
                    for lib in includes
                )
                body = header_lines + "\n" + node.value
            else:
                body = node.value
            with os.fdopen(fd, "w") as f:
                f.write(body)
            self.import_src_files.append(tmp_path)
            if getattr(self.module, "_ccode_src_map", None) is None:
                self.module._ccode_src_map = {}
            src_file = getattr(node, "src_file", None)
            src_line = getattr(node, "src_line", None)
            label = (
                f"{src_file}:{src_line}"
                if src_file and src_line is not None
                else (src_file or "main")
            )
            self.module._ccode_src_map[tmp_path] = label
        return

    @register_emitter(Llvm)
    def emit_llvm(self, node: Llvm):
        """Link an embedded `llvm:` block's IR and declare its functions.

        The block body is written to a temp .ll file appended to
        `import_src_files` so both the JIT and AOT paths link it. For safe
        `llvm:` blocks the semantic pass registers each `define`d function, so
        an extern declaration matching its signature is created here and cpyte
        call sites resolve to it. Unsafe blocks register nothing but are still
        linked verbatim.
        """
        import os
        import tempfile

        if getattr(self, "_llvm_emitted", None) is None:
            self._llvm_emitted = set()
        symbols = getattr(node, "symbols", None)
        key = id(node)
        if node.value:
            names = tuple(sorted(fname for fname, _sig in (symbols or [])))
            key = (names, node.value)
        if key in self._llvm_emitted:
            return
        self._llvm_emitted.add(key)
        if symbols:
            for fname, (ret_type, params, vararg) in symbols:
                if fname in self.functions or fname in self.global_vars:
                    continue
                ret_ty = self.llvm_type(ret_type)
                if isinstance(ret_ty, ir.VoidType) and not params and not vararg:
                    param_tys = []
                else:
                    param_tys = [self.llvm_type(t) for _, t in params]
                fnty = ir.FunctionType(ret_ty, param_tys, var_arg=vararg)
                func = ir.Function(self.module, fnty, name=fname)
                self.functions[fname] = func
        if node.value:
            fd, tmp_path = tempfile.mkstemp(suffix=".ll", prefix="llvm_")
            with os.fdopen(fd, "w") as f:
                f.write(node.value)
            self.import_src_files.append(tmp_path)
        return

    def _emit_combine(self, node):
        """Run the real recursive emitter for a pure node whose children were
        already emitted and memoized during the iterative traversal."""
        self._in_emit_iterative = True
        try:
            v = self._emit_recursive(node)
        finally:
            self._in_emit_iterative = True
        self._emit_memo[id(node)] = v
        return v

    @register_emitter(LLVMblock)
    def emit_llvmblock(self, node: LLVMblock):
        llvm = node.value
        module = binding.parse_assembly(llvm)
        if not node.is_unsafe:
            module.verify()

    def _emit_in_place(self, node):
        """Emit a non-pure node through its normal handler; its deep children
        re-enter the guarded (iterative) path via self.emit."""
        saved = self._in_emit_iterative
        self._in_emit_iterative = False
        try:
            v = self._emit_recursive(node)
        finally:
            self._in_emit_iterative = saved
        self._emit_memo[id(node)] = v
        return v

    def _emit_and_or_step(self, op, node, lhs):
        """One short-circuit step, mirroring emit_binop's AND/OR branches."""
        lhs_true = self._truthy_expr(node.left)
        entry_bb = self.builder.block
        suffix = "and" if op == TokenType.AND else "or"
        rhs_bb = self.builder.append_basic_block(f"{suffix}.rhs")
        end_bb = self.builder.append_basic_block(f"{suffix}.end")
        if op == TokenType.AND:
            self.builder.cbranch(lhs_true, rhs_bb, end_bb)
        else:
            self.builder.cbranch(lhs_true, end_bb, rhs_bb)
        self.builder.position_at_end(rhs_bb)
        saved = self._in_emit_iterative
        self._in_emit_iterative = False
        try:
            rhs = self.emit(node.right)
        finally:
            self._in_emit_iterative = saved
        rhs_true = self._truthy_expr(node.right)
        actual_rhs_bb = self.builder.block
        self.builder.branch(end_bb)
        self.builder.position_at_end(end_bb)
        phi = self.builder.phi(_i1)
        if op == TokenType.AND:
            phi.add_incoming(ir.Constant(_i1, 0), entry_bb)
            phi.add_incoming(rhs_true, actual_rhs_bb)
        else:
            phi.add_incoming(ir.Constant(_i1, 1), entry_bb)
            phi.add_incoming(rhs_true, actual_rhs_bb)
        return phi

    def _emit_and_or_chain(self, node):
        """Emit a left-leaning run of same-op `and`/`or` short-circuit nodes
        iteratively (down the left spine), producing the same phi structure as
        the recursive emit_binop."""
        chain = []
        cur = node
        while isinstance(cur, BinOp) and cur.op in (TokenType.AND, TokenType.OR):
            chain.append(cur)
            if isinstance(cur.left, BinOp) and cur.left.op == cur.op:
                cur = cur.left
            else:
                break
        base = chain[-1].left
        saved = self._in_emit_iterative
        self._in_emit_iterative = False
        try:
            lhs = self.emit(base)
            for n in reversed(chain):
                lhs = self._emit_and_or_step(n.op, n, lhs)
        finally:
            self._in_emit_iterative = saved
        self._emit_memo[id(node)] = lhs
        return lhs

    def _emit_iterative(self, node):
        """Fully iterative post-order expression emitter used when recursion
        depth exceeds `_EMIT_DEPTH_LIMIT`. Pure nodes are combined in
        post-order with memoized child values; `and`/`or` chains are emitted
        iteratively to preserve short-circuit semantics. Memo entries are
        scoped to this invocation so re-emitted nodes (loop conditions) never
        reuse stale values."""
        if id(node) in self._emit_memo:
            return self._emit_memo[id(node)]
        start = set(self._emit_memo)
        try:
            stack = [("visit", node)]
            while stack:
                kind, n = stack.pop()
                key = id(n)
                if key in self._emit_memo:
                    continue
                if isinstance(n, BinOp) and n.op in (TokenType.AND, TokenType.OR):
                    self._emit_and_or_chain(n)
                    continue
                if self._emit_is_pure(n):
                    if kind == "visit":
                        children = _emit_children(n)
                        if children:
                            stack.append(("combine", n))
                            for c in reversed(children):
                                stack.append(("visit", c))
                        else:
                            self._emit_combine(n)
                    else:
                        self._emit_combine(n)
                else:
                    self._emit_in_place(n)
            return self._emit_memo[id(node)]
        finally:
            for k in list(self._emit_memo):
                if k not in start:
                    del self._emit_memo[k]

    def _emit_recursive(self, node: Node | dict) -> _IRValue:
        # Try codegen hooks if extensions are enabled
        if self.enable_extensions:
            for hook in self._hook_registry.get(HookStage.CODEGEN):
                if not isinstance(hook, CodegenHook):
                    continue
                try:
                    if hook.should_emit_node(node):
                        return hook.emit_node(
                            node,
                            self.builder,
                            self._hook_context(data={"node": node}),
                        )
                except Exception as e:
                    raise HookLoadError(
                        f"codegen hook {hook.__class__.__name__} failed: {e}"
                    ) from e

        if isinstance(node, dict):
            if node.get("type") == "for":
                return self.emit_for(node)  # type: ignore[return]
            raise RuntimeError(f"emit: unsupported dict node type {node.get('type')!r}")

        # Registry-based visitor (primary dispatch)
        handler = _EMIT_REGISTRY.get(type(node))
        if handler is not None:
            return handler(self, node)

        # Fallback: dynamic dispatch by naming convention
        method = getattr(self, f"emit_{type(node).__name__.lower()}", None)
        if method is not None:
            return method(node)

        raise RuntimeError(f"emit: no emitter registered for {type(node).__name__}")

    @register_emitter(StructDef)
    def emit_structdef(self, node: StructDef):
        self._struct_nodes[node.name] = node
        if node.generic_params:
            # For generic structs, don't emit the base version with raw type params.
            # Specialized versions are generated on demand by _resolve_generic_type.
            # Still register it so lookup works.
            return
        llvm_struct = self.structs.get(node.name)
        if llvm_struct is None or not isinstance(llvm_struct, ir.IdentifiedStructType):
            # Pre-register an opaque identified struct BEFORE resolving field
            # types so self-referential fields (`BSTNode* left`) resolve to a
            # pointer to this type instead of silently degrading to `i32` (the
            # old behavior corrupted the struct layout). get_identified_type
            # registers into the module's context, so the definition is
            # serialized into the IR (a bare IdentifiedStructType never is).
            llvm_struct = self.module.context.get_identified_type(f"struct.{node.name}")
            self.structs[node.name] = llvm_struct
        field_tys = []
        for f in node.fields:
            field_tys.append(self.llvm_type(f.type_expr))
        if llvm_struct.is_opaque:
            llvm_struct.set_body(*field_tys)
        self.struct_fields[node.name] = node.fields

    @register_emitter(EnumDef)
    def emit_enumdef(self, node: EnumDef):
        # Enum members are constant-folded during semantic analysis and resolved
        # in emit_attr via _enum_member_value; the definition itself emits nothing.
        return None

    @register_emitter(TypeAlias)
    def emit_typealias(self, node: TypeAlias):
        # Type aliases are resolved during semantic analysis; no runtime code.
        return None

    @register_emitter(ClassDef)
    def emit_classdef(self, node: ClassDef):
        """Emit a class as a struct with methods as functions having a hidden 'this' pointer."""
        self._struct_nodes[node.name] = node
        # Collect all fields (including inherited)
        all_fields = []
        if node.base and node.base in self.struct_fields:
            all_fields.extend(self.struct_fields[node.base])
        all_fields.extend(node.fields)
        # Generate struct type
        field_tys = [self.llvm_type(f.type_expr) for f in all_fields]
        if field_tys or all_fields:
            llvm_struct = self.structs.get(node.name)
            if llvm_struct is None or not isinstance(
                llvm_struct, ir.IdentifiedStructType
            ):
                # Pre-register an opaque identified struct BEFORE resolving field
                # types so self-referential fields resolve to a pointer to this
                # type instead of silently degrading to `i32`. get_identified_type
                # registers into the module's context so the definition is
                # serialized into the IR.
                llvm_struct = self.module.context.get_identified_type(
                    f"class.{node.name}"
                )
                self.structs[node.name] = llvm_struct
            if llvm_struct.is_opaque:
                llvm_struct.set_body(*field_tys)
            self.struct_fields[node.name] = all_fields
        # Emit methods — each gets a hidden 'this' pointer as first parameter
        for m in node.methods:
            self._emit_class_method(node, m, all_fields)
            # Emit inherited methods (call through to base class implementation)
        if node.base:
            for fname, fobj in list(self.functions.items()):
                if fname.startswith(node.base + "."):
                    method_name = fname[len(node.base) + 1 :]
                    child_name = f"{node.name}.{method_name}"
                    if child_name not in self.functions:
                        # Create a wrapper that bitcasts the child pointer to base type
                        base_fnty = fobj.function_type
                        child_fnty = ir.FunctionType(
                            base_fnty.return_type,
                            [ir.PointerType(self.structs[node.name])]
                            + list(base_fnty.args[1:]),
                        )
                        wrapper = ir.Function(self.module, child_fnty, child_name)
                        entry = wrapper.append_basic_block("entry")
                        wb = ir.IRBuilder(entry)
                        casted = wb.bitcast(wrapper.args[0], base_fnty.args[0])
                        args = [casted] + list(wrapper.args[1:])
                        ret = wb.call(fobj, args)
                        if base_fnty.return_type == ir.VoidType():
                            wb.ret_void()
                        else:
                            wb.ret(ret)
                        self.functions[child_name] = wrapper

    def _emit_class_method(
        self, class_node: ClassDef, method: FuncDef, all_fields: list
    ):
        """Emit a class method with a hidden 'this' pointer."""
        # Build param types: this* + declared params
        class_ty = self.structs.get(class_node.name)
        if class_ty is None:
            return
        this_ty = ir.PointerType(class_ty)
        non_this_params = {k: v for k, v in method.params.items() if k != "this"}
        param_tys = [this_ty] + [self.llvm_type(t) for t in non_this_params.values()]
        ret_ty = self.llvm_type(method.rettype or "void")
        func_name = f"{class_node.name}.{method.name}"
        fnty = ir.FunctionType(ret_ty, param_tys)
        if func_name in self.functions:
            func = self.functions[func_name]
        else:
            func = ir.Function(self.module, fnty, func_name)
            self.functions[func_name] = func
        entry = func.append_basic_block("entry")
        self.builder = ir.IRBuilder(entry)
        self.builder.position_at_end(entry)
        old_locals = self.locals
        old_local_types = self.local_types
        old_ssa = self.ssa_values
        old_ssa_types = self.ssa_types
        old_scope_stack = self.scope_stack
        self.locals = {}
        self.local_types = {}
        self.ssa_values = {}
        self.ssa_types = {}
        self.scope_stack = [{}]
        # Store 'this' pointer
        this_arg = func.args[0]
        this_ptr = self.builder.alloca(this_ty, name="this")
        self.builder.store(this_arg, this_ptr)
        self.locals["this"] = this_ptr
        self.local_types["this"] = class_node.name + "*"
        # Store method params
        for llvm_arg, (pname, ptype) in zip(func.args[1:], non_this_params.items()):
            ptr = self.builder.alloca(llvm_arg.type, name=pname)
            self.builder.store(llvm_arg, ptr)
            self.locals[pname] = ptr
            self.local_types[pname] = _bc_array_norm(ptype)
        # cpdb: method-entry tracer + debug box
        if self.debug_instrument:
            self._dbg_begin_function(
                func_name, {"this": this_ty} | dict(non_this_params), list(method.body)
            )
            mnid = self.debug_nids.get(id(method))
            if mnid is not None:
                self._dbg_emit_tracer(method, mnid)
        # Emit body
        for stmt in method.body:
            self.emit(stmt)
        if not self._block_terminated():
            if ret_ty == ir.VoidType():
                self.builder.ret_void()
            else:
                self.builder.ret(ir.Constant(ret_ty, 0))
        self.locals = old_locals
        self.local_types = old_local_types
        self.ssa_values = old_ssa
        self.ssa_types = old_ssa_types
        self.scope_stack = old_scope_stack

    @register_emitter(NewExpr)
    def emit_newexpr(self, node: NewExpr):
        if node.type_expr == "str" and node.size is None:
            malloc_fn = self._get_malloc_fn()
            empty_str = self._string_const("")
            ptr = self.builder.call(malloc_fn, [ir.Constant(_i64, 8)])
            i8pp = ir.PointerType(_i8ptr)
            ptr = self.builder.bitcast(ptr, i8pp)
            self.builder.store(empty_str, ptr)
            self._emit_write_barrier(ptr, empty_str)
            return ptr
        # Resolve generic types
        type_str = node.type_expr
        if "<" in type_str and type_str not in self.structs:
            self._resolve_generic_type(type_str)
        if node.size is not None:
            count = self.emit(node.size)
            if isinstance(count.type, ir.PointerType):
                count = self.builder.ptrtoint(count, _i64)
            elif isinstance(count.type, (ir.DoubleType, ir.FloatType)):
                count = self.builder.fptosi(count, _i64)
            elif count.type != _i64:
                count = self.builder.zext(count, _i64)
        else:
            count = ir.Constant(_i64, 1)
        elem_ty = self.llvm_type(node.type_expr)
        if isinstance(elem_ty, ir.VoidType):
            self._codegen_error(
                "cannot allocate `new void` (use `new void*` for a pointer to void)",
                node,
            )
        if _bc_array_back(node.type_expr) is not None:
            elem_ty = self.llvm_type(_bc_array_back(node.type_expr))
        malloc_ty = ir.PointerType(elem_ty)
        elem_size = self._sizeof_type(elem_ty)
        if elem_size.type != count.type:
            elem_size = self.builder.zext(elem_size, count.type)

        # Use length-prefixed header allocation for arrays to avoid hash table lookups
        if node.size is not None:
            alloc_fn = self.functions.get("cpyte_array_alloc")
            if alloc_fn is not None and not self.no_userspace:
                # Call cpyte_array_alloc(elem_size, count)
                if elem_size.type != _i64:
                    elem_size = self.builder.zext(elem_size, _i64)
                ptr = self.builder.call(alloc_fn, [elem_size, count])
                ptr = self.builder.bitcast(ptr, malloc_ty)
                return ptr

        # Fallback to legacy allocation for non-arrays or when cpyte_array_alloc unavailable
        total_size = self.builder.mul(count, elem_size)
        malloc_fn = self._get_malloc_fn()
        ptr = self.builder.call(malloc_fn, [total_size])
        ptr = self.builder.bitcast(ptr, malloc_ty)
        # No registration needed - fallback path doesn't support iteration
        return ptr

    @register_emitter(ListLit)
    def emit_listlit(self, node):
        n = len(node.items)
        elem_size = self._sizeof_type(_DynValue)
        count = ir.Constant(_i64, n)

        # Use length-prefixed header allocation for arrays to avoid hash table lookups
        alloc_fn = self.functions.get("cpyte_array_alloc")
        if alloc_fn is not None and not self.no_userspace:
            if elem_size.type != _i64:
                elem_size = self.builder.zext(elem_size, _i64)
            ptr = self.builder.call(alloc_fn, [elem_size, count])
            ptr = self.builder.bitcast(ptr, _DynValuePtr)
        else:
            # Fallback to legacy allocation
            total_size = self.builder.mul(count, self.builder.zext(elem_size, _i64))
            malloc_fn = self._get_malloc_fn()
            ptr = self.builder.call(malloc_fn, [total_size])
            ptr = self.builder.bitcast(ptr, _DynValuePtr)
            setlen_fn = self.functions.get("cpyte_array_set_len")
            if setlen_fn is not None and not self.no_userspace:
                arr8 = self.builder.bitcast(ptr, _i8ptr)
                self.builder.call(
                    setlen_fn,
                    [
                        arr8,
                        self.builder.zext(count, _i64) if count.type != _i64 else count,
                    ],
                )

        for i, item in enumerate(node.items):
            value = self.emit(item)
            if value.type == _DynValue:
                elem = value
            else:
                kind, bits = self._box_dyn(
                    value, getattr(item, "inferred_type", None) or "int"
                )
                elem = self.builder.insert_value(
                    ir.Constant(_DynValue, ir.Undefined), ir.Constant(_i32, kind), 0
                )
                elem = self.builder.insert_value(elem, bits, 1)
            elem_ptr = self.builder.gep(ptr, [ir.Constant(_i64, i)], inbounds=True)
            self.builder.store(elem, elem_ptr)
            if not self.no_gc and isinstance(value.type, ir.PointerType):
                self._emit_write_barrier(
                    self.builder.bitcast(elem_ptr, _i8ptr),
                    self.builder.bitcast(value, _i8ptr),
                )
        return ptr

    def _get_malloc_fn(self):
        if self.no_gc:
            # Bare-metal: use plain malloc
            fn = self._malloc_fn
            if fn is not None:
                return fn
            for f in self.module.functions:
                if f.name == "malloc":
                    self._malloc_fn = f
                    return f
            fnty = ir.FunctionType(_i8ptr, [_i64])
            fn = ir.Function(self.module, fnty, "malloc")
            self._malloc_fn = fn
            return fn
        # Concurrent tri-color GC allocator
        fn = self._gc_alloc_fn
        if fn is not None:
            return fn
        for f in self.module.functions:
            if f.name == "gc_malloc":
                self._gc_alloc_fn = f
                return f
        fnty = ir.FunctionType(_i8ptr, [_i64])
        fn = ir.Function(self.module, fnty, "gc_malloc")
        self._gc_alloc_fn = fn
        return fn

    def _get_write_barrier_fn(self):
        # Get the gc_write_barrier function for pointer stores between heap objects
        fn = self._gc_write_barrier_fn
        if fn is not None:
            return fn
        for f in self.module.functions:
            if f.name == "gc_write_barrier":
                self._gc_write_barrier_fn = f
                return f
        fnty = ir.FunctionType(_void, [_i8ptr, _i8ptr])
        fn = ir.Function(self.module, fnty, "gc_write_barrier")
        self._gc_write_barrier_fn = fn
        return fn

    def _emit_write_barrier(self, parent_ptr, child_ptr):
        """Insert a write barrier call when storing a pointer value into a heap object.
        parent_ptr and child_ptr must be i8* typed LLVM values."""
        if self.no_gc:
            return
        if parent_ptr.type != _i8ptr:
            parent_ptr = self.builder.bitcast(parent_ptr, _i8ptr)
        if child_ptr.type != _i8ptr:
            child_ptr = self.builder.bitcast(child_ptr, _i8ptr)
        wb_fn = self._get_write_barrier_fn()
        self.builder.call(wb_fn, [parent_ptr, child_ptr])

    def _get_trap_fn(self):
        for f in self.module.functions:
            if f.name == "llvm.trap":
                return f
        fnty = ir.FunctionType(ir.VoidType(), [])
        fn = ir.Function(self.module, fnty, "llvm.trap")
        return fn

    def _type_abi_info(self, ty):
        if isinstance(ty, ir.IntType):
            w = ty.width // 8
            return (w, w)
        if isinstance(ty, ir.DoubleType):
            return (8, 8)
        if isinstance(ty, ir.PointerType):
            return (8, 8)
        if isinstance(ty, ir.ArrayType):
            cnt = ty.count
            elem_sz, elem_align = self._type_abi_info(ty.element)
            return (elem_sz * cnt, elem_align)
        if isinstance(ty, (ir.LiteralStructType, ir.IdentifiedStructType)):
            if hasattr(ty, "elements") and ty.elements is not None:
                max_align = 0
                total = 0
                for el in ty.elements:
                    el_sz, el_align = self._type_abi_info(el)
                    max_align = max(max_align, el_align)
                    if el_align > 0 and total % el_align != 0:
                        total += el_align - (total % el_align)
                    total += el_sz
                if max_align > 0 and total % max_align != 0:
                    total += max_align - (total % max_align)
                return (total, max_align if max_align > 0 else 1)
        return (4, 4)

    def _sizeof_type(self, ty):
        key = id(ty)
        if key in self._sizeof_cache:
            return self._sizeof_cache[key]
        sz, _ = self._type_abi_info(ty)
        c = ir.Constant(ir.IntType(32), sz)
        self._sizeof_cache[key] = c
        return c

    @register_emitter(Deref)
    def emit_deref(self, node: Deref):
        ptr = self.emit(node.operand)
        return self.builder.load(ptr)

    @register_emitter(AddrOf)
    def emit_addrof(self, node: AddrOf):
        if isinstance(node.operand, Variable):
            name = node.operand.name
            ptr = self.locals.get(name)
            if ptr is not None:
                return ptr
            ssa = self.ssa_values.pop(name, None)
            if ssa is not None:
                ptr = self._alloca(ssa.type, name)
                self.builder.store(ssa, ptr)
                self.locals[name] = ptr
                return ptr
            raise Exception(f"Undefined variable '{name}'")
        raise Exception("Address-of requires a variable")

    @register_emitter(BorrowExpr)
    def emit_borrow(self, node: BorrowExpr):
        # `borrow x` / `borrow mut x` lowers to taking x's address, exactly like
        # `&x`. Mutable vs immutable is enforced at the semantic stage.
        return self.emit_addrof(AddrOf(node.operand))

    @register_emitter(MoveExpr)
    def emit_move(self, node: MoveExpr):
        # `move x` is a compile-time ownership transfer; at runtime it just
        # yields the value of x (no copy is made).
        return self.emit(node.operand)

    @register_emitter(SizeOf)
    def emit_sizeof(self, node: SizeOf):
        ty = self.llvm_type(node.type_expr)
        return self._sizeof_type(ty)

    @register_emitter(CastExpr)
    def emit_cast(self, node: CastExpr):
        val = self.emit(node.expr)
        target = self.llvm_type(node.type_expr)
        if val.type == target:
            return val
        cast_ty = node.type_expr
        if cast_ty == "ubig" or cast_ty == "big":
            # Casting any integer/pointer source into an arbitrary-precision big.
            # ubig treats the source as unsigned (zero-extend); big sign-extends.
            src_t = getattr(node.expr, "inferred_type", None)
            if cast_ty == "ubig":
                return self._promote_to_ubig(val)
            return self._promote_to_big(val, src_t)
        if isinstance(val.type, ir.IntType) and isinstance(target, ir.IntType):
            if val.type.width < target.width:
                return self.builder.sext(val, target)
            elif val.type.width > target.width:
                return self.builder.trunc(val, target)
            return val
        if isinstance(val.type, ir.IntType) and isinstance(target, ir.DoubleType):
            return self.builder.sitofp(val, target)
        if isinstance(val.type, ir.DoubleType) and isinstance(target, ir.IntType):
            return self.builder.fptosi(val, target)
        if isinstance(val.type, ir.PointerType) and isinstance(target, ir.PointerType):
            return self.builder.bitcast(val, target)
        if isinstance(val.type, ir.PointerType) and isinstance(target, ir.IntType):
            return self.builder.ptrtoint(val, target)
        if isinstance(val.type, ir.IntType) and isinstance(target, ir.PointerType):
            return self.builder.inttoptr(val, target)
        if isinstance(target, ir.VoidType):
            self._codegen_error(
                "cannot cast to type `void` (use `void*` for a pointer to void)",
                node,
            )
        return self.builder.bitcast(val, target)

    @register_emitter(InlineAsm)
    def emit_inlineasm(self, node: InlineAsm):
        arg_tys = []
        arg_vals = []
        for _, arg_expr in node.inputs:
            val = self.emit(arg_expr)
            arg_tys.append(val.type)
            arg_vals.append(val)
        ret_ty = _void
        if node.outputs:
            out_var = node.outputs[0][1]
            if isinstance(out_var, Variable):
                ptr = self._emit_lvalue(out_var)
                loaded = self.builder.load(ptr)
                ret_ty = loaded.type
        out_constraints = [c for c, _ in node.outputs]
        in_constraints = [c for c, _ in node.inputs]
        clobbers = list(node.clobbers)
        constraint_parts = []
        if out_constraints:
            constraint_parts.append(",".join(out_constraints))
        if in_constraints:
            constraint_parts.append(",".join(in_constraints))
        if clobbers:
            constraint_parts.append(",".join("~" + c for c in clobbers))
        asm_constraints = ",".join(p for p in constraint_parts if p)
        asm_fn_type = ir.FunctionType(ret_ty, arg_tys)
        asm_fn = ir.InlineAsm(
            asm_fn_type, node.template, asm_constraints, side_effect=node.volatile
        )
        call = self.builder.call(asm_fn, arg_vals)
        if node.outputs and isinstance(node.outputs[0][1], Variable):
            ptr = self._emit_lvalue(node.outputs[0][1])
            self.builder.store(call, ptr)
        return call

    @register_emitter(Index)
    def emit_index(self, node: Index):
        if not self.no_userspace:
            obj_t = _bc_array_norm(getattr(node.obj, "inferred_type", None))
            if (
                obj_t == "dynamic"
                or obj_t == "dynamic[]"
                or self._is_dynamic_expr(node.obj)
            ):
                return self._emit_dyn_index(node)
        ptr = self._emit_lvalue(node)
        return self.builder.load(ptr)

    def _emit_dyn_list_ptr(self, node, idx_node):
        """Compute a _DynValue* element pointer for a dynamic list index."""
        if self._is_dynamic_expr(node):
            k, b = self._dyn_pair(node)
            fn = self.functions.get("dyn_list_get")
            idx = self.emit(idx_node)
            if isinstance(idx.type, ir.PointerType):
                idx = self.builder.ptrtoint(idx, _i64)
            elif isinstance(idx.type, (ir.DoubleType, ir.FloatType)):
                idx = self.builder.fptosi(idx, _i64)
            idx64 = self._extend_to_i64(idx) if idx.type.width < 64 else idx
            return self.builder.call(fn, [b, idx64])
        arr = self.emit(node)
        if arr.type != _DynValuePtr:
            arr = self.builder.bitcast(arr, _DynValuePtr)
        idx = self.emit(idx_node)
        if isinstance(idx.type, ir.PointerType):
            idx = self.builder.ptrtoint(idx, _i64)
        elif isinstance(idx.type, (ir.DoubleType, ir.FloatType)):
            idx = self.builder.fptosi(idx, _i64)
        idx64 = self._extend_to_i64(idx) if idx.type.width < 64 else idx
        return self.builder.gep(arr, [idx64], inbounds=True)

    def _emit_dyn_index(self, node: Index):
        elem_ptr = self._emit_dyn_list_ptr(node.obj, node.index)
        return self.builder.load(elem_ptr)

    def _emit_dyn_index_store(self, node: Assign):
        target = node.target
        elem_ptr = self._emit_dyn_list_ptr(target.obj, target.index)
        value = self.emit(node.value)
        if value.type == _DynValue:
            self.builder.store(value, elem_ptr)
            return
        kind, bits = self._box_dyn(
            value, getattr(node.value, "inferred_type", None) or "int"
        )
        s = self.builder.insert_value(
            ir.Constant(_DynValue, ir.Undefined), ir.Constant(_i32, kind), 0
        )
        s = self.builder.insert_value(s, bits, 1)
        self.builder.store(s, elem_ptr)
        return

    def _emit_lvalue_index(self, node: Index):
        obj = self.emit(node.obj)
        idx = self.emit(node.index)
        if isinstance(idx.type, ir.PointerType):
            idx = self.builder.ptrtoint(idx, _i64)
        elif isinstance(idx.type, (ir.DoubleType, ir.FloatType)):
            idx = self.builder.fptosi(idx, _i64)
        # inbounds=True enables LLVM auto-vectorization via strength reduction
        # LLVM will transform base[i] into pointer arithmetic when profitable
        return self.builder.gep(obj, [idx], inbounds=True)

    @register_emitter(Attr)
    def emit_attr(self, node: Attr):
        enum_val = getattr(node, "_enum_member_value", None)
        if enum_val is not None:
            return ir.Constant(ir.IntType(32), enum_val)
        ptr = self._emit_lvalue_attr(node)
        return self.builder.load(ptr)

    def _field_type_name(self, struct_name: str, field_name: str) -> str | None:
        if struct_name not in self.struct_fields:
            return None
        fields = self.struct_fields[struct_name]
        field_idx = None
        for i, f in enumerate(fields):
            if f.name == field_name:
                field_idx = i
                break
        if field_idx is None:
            return None
        struct_type = self.structs.get(struct_name)
        if (
            struct_type
            and hasattr(struct_type, "elements")
            and struct_type.elements is not None
        ):
            elem_tys = struct_type.elements
            if field_idx < len(elem_tys):
                return self._named_type_repr(elem_tys[field_idx])
        return None

    def _named_type_repr(self, ty):
        if not hasattr(self, "_type_names"):
            self._type_names = {}
        if self._type_names:
            return self._type_names.get(ty)
        for name, t in self._iter_named_types():
            self._type_names[t] = name
        return self._type_names.get(ty)

    def _iter_named_types(self):
        yield "int", ir.IntType(32)
        yield "double", ir.DoubleType()
        yield "str", ir.PointerType(ir.IntType(8))
        yield "bool", ir.IntType(1)
        yield "char", ir.IntType(8)
        for name in self.structs:
            yield name, self.structs[name]
        for name in self.structs:
            yield name + "*", ir.PointerType(self.structs[name])

    def _struct_name_from_node(self, node) -> str | None:
        if isinstance(node, Variable):
            declared = self.local_types.get(node.name, "")
            if declared:
                return self._base_type_name(declared)
        if isinstance(node, Deref):
            if isinstance(node.operand, Variable):
                declared = self.local_types.get(node.operand.name, "")
                if declared.endswith("*"):
                    base = declared[:-1]
                    return self._base_type_name(base)
            else:
                sub = self._struct_name_from_node(node.operand)
                if sub:
                    return self._base_type_name(sub)
        if isinstance(node, Attr):
            parent_struct = self._struct_name_from_node(node.obj)
            if parent_struct and parent_struct in self.struct_fields:
                return self._field_type_name(parent_struct, node.name)
        if isinstance(node, Index):
            base = self._struct_name_from_node(node.obj)
            if base:
                return self._base_type_name(base)
            return None
        return None

    def _emit_lvalue_attr(self, node: Attr):
        obj_ptr = self._emit_lvalue(node.obj)
        struct_name = self._struct_name_from_node(node.obj)
        if struct_name:
            # Handle generic struct names
            if "<" in struct_name and struct_name not in self.struct_fields:
                self._resolve_generic_type(struct_name)
            # Try the full generic name first, then base name
            for sn in (struct_name, struct_name.split("<")[0]):
                if sn and sn in self.struct_fields:
                    if isinstance(
                        getattr(obj_ptr.type, "pointee", None), ir.PointerType
                    ):
                        obj_ptr = self.builder.load(obj_ptr)
                    fields = self.struct_fields[sn]
                    for i, f in enumerate(fields):
                        if f.name == node.name:
                            return self.builder.gep(
                                obj_ptr,
                                [ir.Constant(_i32, 0), ir.Constant(_i32, i)],
                                inbounds=True,
                            )
        raise Exception(f"Unknown field '{node.name}' in struct '{struct_name}'")

    def _emit_lvalue(self, node):
        if isinstance(node, Variable):
            name = node.name
            ptr = self.locals.get(name)
            if ptr is not None:
                return ptr
            ssa = self.ssa_values.get(name)
            if ssa is not None:
                ptr = self._alloca(ssa.type, name)
                self.builder.store(ssa, ptr)
                self.locals[name] = ptr
                return ptr
            raise Exception(f"Undefined variable '{name}'")
        if isinstance(node, Index):
            return self._emit_lvalue_index(node)
        if isinstance(node, Attr):
            return self._emit_lvalue_attr(node)
        if isinstance(node, Deref):
            return self.emit(node.operand)
        raise Exception("Cannot take address of expression")

    @register_emitter(FuncDef)
    def emit_funcdef(self, node: FuncDef):
        if node.decorators and node.name != "main":
            return self._emit_decorated_funcdef(node)
        ret_ty = self.llvm_type(node.rettype or "int")
        param_tys = [self.llvm_type(t) for t in node.params.values()]
        self._check_params_no_void(param_tys, list(node.params.keys()), node)

        if node.name in self.functions:
            func = self.functions[node.name]
        else:
            func = ir.Function(
                self.module, ir.FunctionType(ret_ty, param_tys), name=node.name
            )
            self.functions[node.name] = func

        entry = func.append_basic_block("entry")
        self.builder = ir.IRBuilder(entry)
        self.builder.position_at_end(entry)

        # Initialize concurrent tri-color GC in main function
        if node.name == "main" and not self.no_gc:
            gc_init_fn = self.functions.get("gc_init")
            if gc_init_fn:
                self.builder.call(gc_init_fn, [])
            gc_start_fn = self.functions.get("gc_start_thread")
            if gc_start_fn:
                self.builder.call(gc_start_fn, [])

        old_locals = self.locals
        old_local_types = self.local_types
        old_ssa = self.ssa_values
        old_ssa_types = self.ssa_types
        old_scope_stack = self.scope_stack
        old_deferred = self._deferred
        old_in_decorator = self._in_decorator
        old_decorator_factory = self._is_decorator_factory
        old_func_rettype = self._func_rettype
        old_const_prop = self._const_prop
        old_const_prop_f = self._const_prop_f
        self._in_decorator = node.rettype == "decorated"
        self._is_decorator_factory = node.rettype == "decorated"
        self._func_rettype = node.rettype
        self.locals = {}
        self.local_types = {}
        self.ssa_values = {}
        self.ssa_types = {}
        self._const_prop = {}
        self._const_prop_f = {}
        self.scope_stack = [{}]
        self._deferred = []
        for llvm_arg, (name, ptype) in zip(func.args, node.params.items()):
            if isinstance(llvm_arg.type, ir.VoidType):
                self._codegen_error(
                    f"parameter `{name}` has type `void`; "
                    f"use a pointer type like `void*` instead",
                    node,
                )
            ptr = self.builder.alloca(llvm_arg.type, name=name)
            self.builder.store(llvm_arg, ptr)
            self.locals[name] = ptr
            self.local_types[name] = _bc_array_norm(ptype)

        # ---- cpdb: function-entry tracer + debug-box setup ----
        if self.debug_instrument:
            self._dbg_begin_function(node.name, node.params, list(node.body))
            fnid = self.debug_nids.get(id(node))
            if fnid is not None:
                self._dbg_emit_tracer(node, fnid)

        # ---- 1.  Fibonacci fast-doubling pattern replacement ----
        if self.debug_instrument or not self._try_emit_fibonacci(
            node, func, ret_ty, param_tys
        ):
            pending_ivs: dict = {}
            for stmt in node.body:
                if not self._block_terminated():
                    if isinstance(stmt, While) and not self.debug_instrument:
                        unrolled = self._try_unroll_counted_loop(stmt, pending_ivs)
                        # Any while invalidates every pending induction
                        # variable (its body may take addresses / write any name).
                        pending_ivs.clear()
                        if not unrolled:
                            self.emit(stmt)
                    elif isinstance(stmt, VarDecl) and isinstance(stmt.init, Number):
                        v = _const_int_value(stmt.init)
                        if v is not None:
                            pending_ivs[stmt.name] = v
                        else:
                            pending_ivs.pop(stmt.name, None)
                        self.emit(stmt)
                    else:
                        pending_ivs.clear()
                        self.emit(stmt)

        if not self._block_terminated():
            self._run_deferred()
            # Shutdown GC before main returns
            if node.name == "main" and not self.no_gc:
                gc_shutdown_fn = self.functions.get("gc_shutdown")
                if gc_shutdown_fn:
                    self.builder.call(gc_shutdown_fn, [])
            if isinstance(ret_ty, ir.VoidType):
                self.builder.ret_void()
            elif ret_ty == _DynValue:
                out = self.builder.insert_value(
                    ir.Constant(_DynValue, ir.Undefined),
                    ir.Constant(_i32, _DYN_NONE),
                    0,
                )
                out = self.builder.insert_value(out, ir.Constant(_i64, 0), 1)
                self.builder.ret(out)
            elif isinstance(ret_ty, ir.PointerType):
                self.builder.ret(ir.Constant(ret_ty, None))
            else:
                self.builder.ret(ir.Constant(ret_ty, 0))

        self.locals = old_locals
        self.local_types = old_local_types
        self.ssa_values = old_ssa
        self.ssa_types = old_ssa_types
        self.scope_stack = old_scope_stack
        self._deferred = old_deferred
        self._in_decorator = old_in_decorator
        self._is_decorator_factory = old_decorator_factory
        self._func_rettype = old_func_rettype
        self._const_prop = old_const_prop
        self._const_prop_f = old_const_prop_f

    def _try_emit_fibonacci(self, node: FuncDef, func, ret_ty, param_tys) -> bool:
        """Detect a naive recursive Fibonacci function and replace it with a
        fast-doubling implementation (`F(2k) = F(k)(2F(k+1) - F(k))`,
        `F(2k+1) = F(k+1)^2 + F(k)^2`), which reduces the number of bigint
        multiplications from O(N) to O(log N).

        For i32/i64 return types a closed-form lookup table is used for small
        N (N <= 70) to keep the fast path trivial.
        """
        fib = _detect_fibonacci_pattern(node)
        if fib is None:
            return False
        param_name, _param_type, _ret_type = fib

        # Bail if the parameter is not an integer type (pointer / str / big
        # params are not valid for the fibonacci specialisation).
        param_alloca = self.locals[param_name]
        if not isinstance(param_alloca.type.pointee, ir.IntType):
            return False

        # We must have emitted nothing for the body yet — we're at the top of
        # the function body loop.

        # Parameter local:
        arg_ptr = self.locals[param_name]
        n_val = self.builder.load(arg_ptr, param_name)
        # Widen to i64 for the algorithm.
        n64 = self._promote_int(n_val, _i64)

        is_i64_ret = ret_ty == _i64
        is_i32_ret = ret_ty == _i32
        is_big_ret = isinstance(ret_ty, ir.PointerType)

        if is_big_ret:
            # ---- Bigint fast-doubling via runtime helpers. ----
            i64_0 = ir.Constant(_i64, 0)
            i64_1 = ir.Constant(_i64, 1)
            i64_2 = ir.Constant(_i64, 2)

            big_const = self.functions.get("bigint_from_int")
            big_sub = self.functions.get("bigint_sub")
            big_add = self.functions.get("bigint_add")
            big_mul = self.functions.get("bigint_mul")
            big_cmp = self.functions.get("bigint_cmp")

            if not (big_const and big_sub and big_add and big_mul and big_cmp):
                return False

            # ---- Fast-doubling loop: a,b = 1,1; iterate over bits of n ----
            # (a, b) = (F(1), F(2)): the leading 1 bit has already been
            # consumed, so the mask starts below it (mask = 1 << (bits - 2)).
            a_ptr = self._alloca(_i8ptr, name="fib.a")
            b_ptr = self._alloca(_i8ptr, name="fib.b")
            n_ptr = self._alloca(_i64, name="fib.n")
            self.builder.store(self.builder.call(big_const, [i64_1]), a_ptr)
            self.builder.store(self.builder.call(big_const, [i64_1]), b_ptr)
            self.builder.store(n64, n_ptr)

            # Compute bit length of n in-loop (count bits by shifting right).
            #   bits = 0; tmp = n; while (tmp > 0) { tmp >>= 1; bits++; }

            # ---- Compute bit length (while tmp>0: bits++) ----
            bitlen_ptr = self._alloca(_i64, name="fib.bits")
            tmp_ptr = self._alloca(_i64, name="fib.tmp")
            self.builder.store(i64_0, bitlen_ptr)
            self.builder.store(n64, tmp_ptr)

            bits_cond = self.builder.append_basic_block("fib.blen.cond")
            bits_body = self.builder.append_basic_block("fib.blen.body")
            bits_end = self.builder.append_basic_block("fib.blen.end")
            self.builder.branch(bits_cond)
            self.builder.position_at_end(bits_cond)
            tmp_v = self.builder.load(tmp_ptr)
            self.builder.cbranch(
                self.builder.icmp_signed(">", tmp_v, i64_0), bits_body, bits_end
            )
            self.builder.position_at_end(bits_body)
            tmp_v2 = self.builder.load(tmp_ptr)
            self.builder.store(self.builder.ashr(tmp_v2, i64_1), tmp_ptr)
            bits_v = self.builder.load(bitlen_ptr)
            self.builder.store(self.builder.add(bits_v, i64_1), bitlen_ptr)
            self.builder.branch(bits_cond)
            self.builder.position_at_end(bits_end)

            # bits = bitlen
            bits = self.builder.load(bitlen_ptr)

            # If n <= 1: return big(n) directly.
            small_bb = self.builder.append_basic_block("fib.small")
            big_bb = self.builder.append_basic_block("fib.big")
            fib_end = self.builder.append_basic_block("fib.end")
            n_smallv = self.builder.load(n_ptr)
            self.builder.cbranch(
                self.builder.icmp_signed("<=", n_smallv, i64_1),
                small_bb,
                big_bb,
            )

            self.builder.position_at_end(small_bb)
            n_for_small = self.builder.load(n_ptr)
            small_ret = self.builder.call(big_const, [n_for_small])
            self.builder.branch(fib_end)

            # Big path: fast-doubling from just below the MSB down to 0.
            # (a, b) starts at (F(1), F(2)) = (1, 1).
            self.builder.position_at_end(big_bb)
            # idx_ptr keeps the loop counter; mask = 1 << (bits - 2)
            idx_ptr = self._alloca(_i64, name="fib.i")
            mask_ptr = self._alloca(_i64, name="fib.mask")

            # mask = 1 << (bits - 2)
            one_shift = self.builder.sub(bits, ir.Constant(_i64, 2))
            mask_init = self.builder.shl(i64_1, one_shift)
            self.builder.store(mask_init, mask_ptr)

            fd_cond = self.builder.append_basic_block("fib.fd.cond")
            fd_body = self.builder.append_basic_block("fib.fd.body")
            fd_end = self.builder.append_basic_block("fib.fd.end")
            self.builder.branch(fd_cond)
            self.builder.position_at_end(fd_cond)
            mask_v = self.builder.load(mask_ptr)
            self.builder.cbranch(
                self.builder.icmp_signed(">", mask_v, i64_0), fd_body, fd_end
            )
            self.builder.position_at_end(fd_body)

            a = self.builder.load(a_ptr)
            b = self.builder.load(b_ptr)

            # c = a * (2*b - a)
            two_b = self.builder.call(big_add, [b, b])
            two_b_minus_a = self.builder.call(big_sub, [two_b, a])
            c = self.builder.call(big_mul, [a, two_b_minus_a])
            # d = a*a + b*b
            a_sq = self.builder.call(big_mul, [a, a])
            b_sq = self.builder.call(big_mul, [b, b])
            d = self.builder.call(big_add, [a_sq, b_sq])

            # if (n & mask) != 0:
            n_val2 = self.builder.load(n_ptr)
            mask_v2 = self.builder.load(mask_ptr)
            bit = self.builder.and_(n_val2, mask_v2)
            bit_nonzero = self.builder.icmp_signed("!=", bit, i64_0)

            set_bb = self.builder.append_basic_block("fib.set")
            clear_bb = self.builder.append_basic_block("fib.clear")
            step_end = self.builder.append_basic_block("fib.step.end")
            self.builder.cbranch(bit_nonzero, set_bb, clear_bb)

            # bit set: a=d, b=c+d
            self.builder.position_at_end(set_bb)
            new_a = d
            new_b = self.builder.call(big_add, [c, d])
            self.builder.store(new_a, a_ptr)
            self.builder.store(new_b, b_ptr)
            self.builder.branch(step_end)

            # bit clear: a=c, b=d
            self.builder.position_at_end(clear_bb)
            self.builder.store(c, a_ptr)
            self.builder.store(d, b_ptr)
            self.builder.branch(step_end)

            self.builder.position_at_end(step_end)
            mask_next = self.builder.load(mask_ptr)
            self.builder.store(self.builder.ashr(mask_next, i64_1), mask_ptr)
            self.builder.branch(fd_cond)

            self.builder.position_at_end(fd_end)
            final_a = self.builder.load(a_ptr)
            self.builder.branch(fib_end)

            self.builder.position_at_end(fib_end)
            phi = self.builder.phi(_i8ptr)
            phi.add_incoming(small_ret, small_bb)
            phi.add_incoming(final_a, fd_end)
            self.builder.ret(phi)
            return True

        # ---- Fixed-width (i32 / i64): closed-form lookup table ----
        width = ret_ty.width if isinstance(ret_ty, ir.IntType) else 32
        max_lookup = 70
        # n <= max_lookup -> table[n]
        # else -> run fast-doubling on i64 arithmetic.

        small_n_limit = ir.Constant(_i64, max_lookup)
        # Negative N keeps the original semantics (`return n` for n <= 1),
        # so only route 0..70 through the table.
        is_neg = self.builder.icmp_signed("<", n64, ir.Constant(_i64, 0))
        is_small = self.builder.icmp_signed("<=", n64, small_n_limit)

        check_bb = self.builder.append_basic_block("fib.check")
        small_bb = self.builder.append_basic_block("fib.small")
        big_bb = self.builder.append_basic_block("fib.big")
        neg_bb = self.builder.append_basic_block("fib.neg")
        end_bb = self.builder.append_basic_block("fib.end")
        self.builder.cbranch(is_neg, neg_bb, check_bb)

        # 0 <= n64 <= 70 -> table[n] ; otherwise fast-doubling.
        self.builder.position_at_end(check_bb)
        self.builder.cbranch(is_small, small_bb, big_bb)

        # n < 0: mirror the recursive base case (`return n`).
        self.builder.position_at_end(neg_bb)
        neg_val = n64
        if neg_val.type != ret_ty:
            neg_val = self.builder.trunc(neg_val, ret_ty)
        self.builder.branch(end_bb)

        # Build a lookup table of i64 constants F(0)..F(70)
        table_len = max_lookup + 1
        consts = [ir.Constant(_i64, _FIB_SMALL_TABLE[i]) for i in range(table_len)]
        arr_ty = ir.ArrayType(_i64, table_len)
        table_gv = ir.GlobalVariable(self.module, arr_ty, name=".fib_table")
        table_gv.global_constant = True
        table_gv.initializer = ir.Constant(arr_ty, consts)

        self.builder.position_at_end(small_bb)
        n_small = self.builder.load(arg_ptr, param_name)
        n_small64 = self._promote_int(n_small, _i64)
        elem_ptr = self.builder.gep(
            table_gv, [ir.Constant(_i32, 0), n_small64], inbounds=True
        )
        small_val = self.builder.load(elem_ptr)
        if ret_ty != _i64:
            small_val = self.builder.trunc(small_val, ret_ty)
        self.builder.branch(end_bb)

        # Large-N: iterative fast-doubling using i64 arithmetic.
        self.builder.position_at_end(big_bb)
        a2_ptr = self._alloca(_i64, name="fib.a2")
        b2_ptr = self._alloca(_i64, name="fib.b2")
        self.builder.store(ir.Constant(_i64, 1), a2_ptr)
        self.builder.store(ir.Constant(_i64, 1), b2_ptr)
        n2 = self.builder.load(arg_ptr, param_name)
        n2_64 = self._promote_int(n2, _i64)

        # Compute bit length
        bl_ptr = self._alloca(_i64, name="fib.bl")
        tmp_ptr = self._alloca(_i64, name="fib.tmp2")
        self.builder.store(ir.Constant(_i64, 0), bl_ptr)
        self.builder.store(n2_64, tmp_ptr)
        bl_cond = self.builder.append_basic_block("fib.bl.cond")
        bl_body = self.builder.append_basic_block("fib.bl.body")
        bl_end = self.builder.append_basic_block("fib.bl.end")
        self.builder.branch(bl_cond)
        self.builder.position_at_end(bl_cond)
        t = self.builder.load(tmp_ptr)
        self.builder.cbranch(
            self.builder.icmp_signed(">", t, ir.Constant(_i64, 0)), bl_body, bl_end
        )
        self.builder.position_at_end(bl_body)
        t2 = self.builder.load(tmp_ptr)
        self.builder.store(self.builder.ashr(t2, ir.Constant(_i64, 1)), tmp_ptr)
        blv = self.builder.load(bl_ptr)
        self.builder.store(self.builder.add(blv, ir.Constant(_i64, 1)), bl_ptr)
        self.builder.branch(bl_cond)
        self.builder.position_at_end(bl_end)
        bl = self.builder.load(bl_ptr)

        mask_ptr = self._alloca(_i64, name="fib.mask2")
        one = ir.Constant(_i64, 1)
        mask_shift = self.builder.sub(bl, ir.Constant(_i64, 2))
        self.builder.store(self.builder.shl(one, mask_shift), mask_ptr)

        fd_cond = self.builder.append_basic_block("fib.fd2.cond")
        fd_body = self.builder.append_basic_block("fib.fd2.body")
        fd_end = self.builder.append_basic_block("fib.fd2.end")
        self.builder.branch(fd_cond)
        self.builder.position_at_end(fd_cond)
        mv = self.builder.load(mask_ptr)
        self.builder.cbranch(
            self.builder.icmp_signed(">", mv, ir.Constant(_i64, 0)), fd_body, fd_end
        )
        self.builder.position_at_end(fd_body)

        a2 = self.builder.load(a2_ptr)
        b2 = self.builder.load(b2_ptr)
        two = ir.Constant(_i64, 2)
        # c = a*(2*b - a)
        two_b = self.builder.mul(b2, two)
        two_b_minus_a = self.builder.sub(two_b, a2)
        c2 = self.builder.mul(a2, two_b_minus_a)
        # d = a*a + b*b
        a_sq2 = self.builder.mul(a2, a2)
        b_sq2 = self.builder.mul(b2, b2)
        d2 = self.builder.add(a_sq2, b_sq2)

        n3 = self.builder.load(arg_ptr, param_name)
        n3_64 = self._promote_int(n3, _i64)
        mv2 = self.builder.load(mask_ptr)
        bit2 = self.builder.and_(n3_64, mv2)
        bit_nz = self.builder.icmp_signed("!=", bit2, ir.Constant(_i64, 0))

        set_bb = self.builder.append_basic_block("fib.fd2.set")
        clr_bb = self.builder.append_basic_block("fib.fd2.clr")
        step_end = self.builder.append_basic_block("fib.fd2.step.end")
        self.builder.cbranch(bit_nz, set_bb, clr_bb)

        self.builder.position_at_end(set_bb)
        sum_cd = self.builder.add(c2, d2)
        self.builder.store(d2, a2_ptr)
        self.builder.store(sum_cd, b2_ptr)
        self.builder.branch(step_end)

        self.builder.position_at_end(clr_bb)
        self.builder.store(c2, a2_ptr)
        self.builder.store(d2, b2_ptr)
        self.builder.branch(step_end)

        self.builder.position_at_end(step_end)
        mv3 = self.builder.load(mask_ptr)
        self.builder.store(self.builder.ashr(mv3, one), mask_ptr)
        self.builder.branch(fd_cond)

        self.builder.position_at_end(fd_end)
        big_val = self.builder.load(a2_ptr)
        if ret_ty.width < 64:
            big_val = self.builder.trunc(big_val, ret_ty)
        self.builder.branch(end_bb)

        self.builder.position_at_end(end_bb)
        phi = self.builder.phi(ret_ty)
        phi.add_incoming(neg_val, neg_bb)
        phi.add_incoming(small_val, small_bb)
        phi.add_incoming(big_val, fd_end)
        self.builder.ret(phi)
        return True

    def _emit_decorated_funcdef(self, node: FuncDef):
        """Emit a decorated function: original as __name, trampoline, and wrapper."""
        orig_name = f"__{node.name}"
        trampoline_name = f"__{node.name}_trampoline"
        ret_ty = self.llvm_type(node.rettype or "int")
        param_tys = [self.llvm_type(t) for t in node.params.values()]
        self._check_params_no_void(param_tys, list(node.params.keys()), node)

        # 1. Compile original function body as __name
        orig_func = ir.Function(
            self.module, ir.FunctionType(ret_ty, param_tys), name=orig_name
        )
        self.functions[orig_name] = orig_func

        entry = orig_func.append_basic_block("entry")
        self.builder = ir.IRBuilder(entry)
        self.builder.position_at_end(entry)

        old_locals = self.locals
        old_local_types = self.local_types
        old_ssa = self.ssa_values
        old_ssa_types = self.ssa_types
        old_scope_stack = self.scope_stack
        old_in_decorator = self._in_decorator
        old_decorator_factory = self._is_decorator_factory
        old_decorator_args = self._decorator_args
        old_decorator_func_name = self._decorator_func_name
        old_decorator_params = self._decorator_params
        old_decorator_skip = self._decorator_skip
        self._in_decorator = True
        # The decorated ORIGINAL is NOT a decorator factory: `result`/`args`/
        # `func_name`/`skip` must stay ordinary identifiers in its body.
        self._is_decorator_factory = False
        self._decorator_args = []
        self._decorator_func_name = node.name
        self._decorator_params = list(node.params.keys())
        self._decorator_skip = False
        self.locals = {}
        self.local_types = {}
        self.ssa_values = {}
        self.ssa_types = {}
        self.scope_stack = [{}]

        # ENHANCEMENT: Store argument values for decorator access
        args_array_ty = ir.ArrayType(_DynValue, len(node.params))
        args_array_ptr = self.builder.alloca(args_array_ty, name="decorator_args_array")
        for i, (llvm_arg, (pname, ptype)) in enumerate(
            zip(orig_func.args, node.params.items())
        ):
            ptr = self.builder.alloca(llvm_arg.type, name=pname)
            self.builder.store(llvm_arg, ptr)
            self.locals[pname] = ptr
            self.local_types[pname] = _bc_array_norm(ptype)

            # Box argument and store in args array
            kind, bits = self._box_dyn(llvm_arg, ptype)
            dyn_val = self.builder.insert_value(
                ir.Constant(_DynValue, ir.Undefined), ir.Constant(_i32, kind), 0
            )
            dyn_val = self.builder.insert_value(dyn_val, bits, 1)
            elem_ptr = self.builder.gep(
                args_array_ptr, [ir.Constant(_i64, 0), ir.Constant(_i64, i)]
            )
            self.builder.store(dyn_val, elem_ptr)
            self._decorator_args.append((pname, ptype))

        # ENHANCEMENT: Make args array accessible via 'args' variable
        self.locals["args"] = args_array_ptr
        self.local_types["args"] = f"dynamic[{len(node.params)}]"

        # ENHANCEMENT: Make function name accessible via 'func_name' variable
        func_name_str = self._string_const(node.name)
        func_name_ptr = self.builder.alloca(_i8ptr, name="func_name")
        self.builder.store(func_name_str, func_name_ptr)
        self.locals["func_name"] = func_name_ptr
        self.local_types["func_name"] = "str"

        for stmt in node.body:
            if not self._block_terminated():
                self.emit(stmt)

        if not self._block_terminated():
            if isinstance(ret_ty, ir.VoidType):
                self.builder.ret_void()
            elif node.decorators:
                # Decorator function: auto-return @__code_result
                result_val = self.builder.load(self._code_result, "dec_result")
                self.builder.ret(result_val)
            elif ret_ty == _DynValue:
                out = self.builder.insert_value(
                    ir.Constant(_DynValue, ir.Undefined),
                    ir.Constant(_i32, _DYN_NONE),
                    0,
                )
                out = self.builder.insert_value(out, ir.Constant(_i64, 0), 1)
                self.builder.ret(out)
            elif isinstance(ret_ty, ir.PointerType):
                self.builder.ret(ir.Constant(ret_ty, None))
            else:
                self.builder.ret(ir.Constant(ret_ty, 0))

        self.locals = old_locals
        self.local_types = old_local_types
        self.ssa_values = old_ssa
        self.ssa_types = old_ssa_types
        self.scope_stack = old_scope_stack
        self._in_decorator = old_in_decorator
        self._is_decorator_factory = old_decorator_factory
        self._decorator_args = old_decorator_args
        self._decorator_func_name = old_decorator_func_name
        self._decorator_params = old_decorator_params
        self._decorator_skip = old_decorator_skip

        # 2. Generate trampoline: calls __name and returns DynValue.
        # The signature is () -> DynValue so decorator factories can call it via
        # the __code_fn pointer without knowing the decorated signature. The
        # incoming args are pulled from the __code_args global (filled by the
        # wrapper) and unboxed here where the param types are static.
        tramp_fnty = ir.FunctionType(_DynValue, [])
        tramp = ir.Function(self.module, tramp_fnty, trampoline_name)
        self.functions[trampoline_name] = tramp
        tramp_entry = tramp.append_basic_block("entry")
        self.builder = ir.IRBuilder(tramp_entry)
        self.builder.position_at_end(tramp_entry)

        args_ptr = self.builder.load(self._code_args, "code_args_ptr")
        call_args = []
        for i, (pname, ptype) in enumerate(node.params.items()):
            elem_ptr = self.builder.gep(args_ptr, [ir.Constant(_i64, i)], inbounds=True)
            dyn_val = self.builder.load(elem_ptr, pname)
            kind = self.builder.extract_value(dyn_val, 0)
            bits = self.builder.extract_value(dyn_val, 1)
            want = self._dyn_kind_for(ptype)
            as_v = self.builder.call(
                self.functions.get("dyn_as_v"),
                [kind, bits, ir.Constant(_i32, want)],
            )
            call_args.append(self._unbox_dyn(as_v, ptype))
        call_result = self.builder.call(orig_func, call_args)
        if call_result.type == _DynValue:
            self.builder.store(call_result, self._code_result)
            self.builder.ret(call_result)
        else:
            kind, bits = self._box_dyn(call_result, node.rettype)
            dyn_val = self.builder.insert_value(
                ir.Constant(_DynValue, ir.Undefined), ir.Constant(_i32, kind), 0
            )
            dyn_val = self.builder.insert_value(dyn_val, bits, 1)
            self.builder.store(dyn_val, self._code_result)
            self.builder.ret(dyn_val)

        # 3. Generate wrapper: same name/signature as original
        wrapper_fnty = ir.FunctionType(ret_ty, param_tys)
        wrapper = ir.Function(self.module, wrapper_fnty, node.name)
        self.functions[node.name] = wrapper
        wrap_entry = wrapper.append_basic_block("entry")
        self.builder = ir.IRBuilder(wrap_entry)
        self.builder.position_at_end(wrap_entry)

        # Box the wrapper's incoming args and store them (plus the decorated
        # function's name and a fresh skip flag) into the decorator-access
        # globals. The trampoline and any decorator factory body read these at
        # runtime.
        args_buf = self.builder.alloca(
            ir.ArrayType(_DynValue, len(node.params)), name="decorator_args_buf"
        )
        for i, (llvm_arg, (pname, ptype)) in enumerate(
            zip(wrapper.args, node.params.items())
        ):
            kind, bits = self._box_dyn(llvm_arg, ptype)
            dyn_val = self.builder.insert_value(
                ir.Constant(_DynValue, ir.Undefined), ir.Constant(_i32, kind), 0
            )
            dyn_val = self.builder.insert_value(dyn_val, bits, 1)
            elem_ptr = self.builder.gep(
                args_buf,
                [ir.Constant(_i64, 0), ir.Constant(_i64, i)],
                inbounds=True,
            )
            self.builder.store(dyn_val, elem_ptr)
        self.builder.store(
            self.builder.bitcast(args_buf, _DynValuePtr), self._code_args
        )
        self.builder.store(self._string_const(node.name), self._code_fn_name)
        self.builder.store(ir.Constant(ir.IntType(1), 0), self._code_skip)

        # Store trampoline pointer into __code_fn
        tramp_i8 = self.builder.bitcast(tramp, _i8ptr)
        self.builder.store(tramp_i8, self._code_fn_ptr)

        # Call each decorator
        for dec_expr in node.decorators:
            self.emit(dec_expr)

        # Check skip flag - if a decorator factory body wrote `skip = true`,
        # return early without calling the original via the trampoline.
        skip_val = self.builder.load(self._code_skip, "dec_skip")
        skip_bb = wrapper.append_basic_block("skip_original")
        call_bb = wrapper.append_basic_block("call_original")
        self.builder.cbranch(skip_val, skip_bb, call_bb)

        self.builder.position_at_end(skip_bb)
        # Return default value when skipped
        if isinstance(ret_ty, ir.VoidType):
            self.builder.ret_void()
        elif ret_ty == _DynValue:
            default_dyn = self.builder.insert_value(
                ir.Constant(_DynValue, ir.Undefined), ir.Constant(_i32, _DYN_NONE), 0
            )
            default_dyn = self.builder.insert_value(
                default_dyn, ir.Constant(_i64, 0), 1
            )
            self.builder.ret(default_dyn)
        elif isinstance(ret_ty, ir.PointerType):
            self.builder.ret(ir.Constant(ret_ty, None))
        else:
            self.builder.ret(ir.Constant(ret_ty, 0))

        self.builder.position_at_end(call_bb)
        # Load __code_result and return it
        result_val = self.builder.load(self._code_result, "dec_result")
        ret_val = self._unbox_dyn(
            self.builder.extract_value(result_val, 1),
            node.rettype or "int",
        )
        self.builder.ret(ret_val)

    @register_emitter(Return)
    def emit_return(self, node: Return):
        if self._block_terminated():
            return
        self._run_deferred()
        # Shutdown GC before main returns
        fn_name = self.builder.function.name
        if fn_name == "main" and not self.no_gc:
            gc_shutdown_fn = self.functions.get("gc_shutdown")
            if gc_shutdown_fn:
                self.builder.call(gc_shutdown_fn, [])
        if node.value is not None:
            value = self.emit(node.value)
            ret_ty = self.builder.function.ftype.return_type
            if ret_ty == _DynValue:
                if value.type != _DynValue:
                    kind, bits = self._box_dyn(
                        value, getattr(node.value, "inferred_type", None)
                    )
                    value = self.builder.insert_value(
                        ir.Constant(_DynValue, ir.Undefined), ir.Constant(_i32, kind), 0
                    )
                    value = self.builder.insert_value(value, bits, 1)
                self.builder.ret(value)
                return
            if value.type != ret_ty:
                if isinstance(value.type, ir.IntType) and isinstance(
                    ret_ty, ir.IntType
                ):
                    if value.type.width < ret_ty.width:
                        value = self.builder.zext(value, ret_ty)
                    elif value.type.width > ret_ty.width:
                        value = self.builder.trunc(value, ret_ty)
                elif isinstance(value.type, ir.PointerType) and isinstance(
                    ret_ty, ir.IntType
                ):
                    value = self.builder.ptrtoint(value, ret_ty)
                elif isinstance(value.type, ir.IntType) and isinstance(
                    ret_ty, ir.PointerType
                ):
                    if self._func_rettype == "big":
                        value = self._promote_to_big(
                            value, getattr(node.value, "inferred_type", None)
                        )
                    elif self._func_rettype == "ubig":
                        value = self._promote_to_ubig(value)
                    else:
                        value = self.builder.inttoptr(value, ret_ty)
            self.builder.ret(value)
        else:
            ret_ty = self.builder.function.ftype.return_type
            if isinstance(ret_ty, ir.VoidType):
                self.builder.ret_void()
            elif isinstance(ret_ty, ir.PointerType):
                self.builder.ret(ir.Constant(ret_ty, None))
            else:
                self.builder.ret(ir.Constant(ret_ty, 0))
        return

    @staticmethod
    def _switchable_if(node):
        cases = []
        var_name = None
        var_node = None

        def extract(n):
            nonlocal var_name, var_node
            if not isinstance(n, If):
                return False
            if not isinstance(n.cond, BinOp) or n.cond.op != TokenType.EQ_EQ:
                return False
            if (
                not isinstance(n.cond.left, Variable)
                or getattr(n.cond.left, "dynamic", False)
                or not isinstance(n.cond.right, Number)
            ):
                return False
            if var_name is None:
                var_name = n.cond.left.name
                var_node = n.cond.left
            elif n.cond.left.name != var_name:
                return False
            cases.append((n.cond.right, n.body))
            if (
                isinstance(n.orelse, list)
                and len(n.orelse) == 1
                and isinstance(n.orelse[0], If)
            ):
                return extract(n.orelse[0])
            else:
                if n.orelse:
                    cases.append((None, n.orelse))
                return True

        if not extract(node):
            return None
        if var_name is None:
            return None
        if len(cases) < 2:
            return None
        # Float variables must not be lowered to a switch: the emitter would
        # compare a coerced i1 against double case constants and produce invalid
        # IR (`case value is not a constant integer`), breaking whole-module
        # builds. Fall back to the normal fcmp/icmp path instead.
        var_ty = (var_node.inferred_type or "").strip() if var_node else ""
        if var_ty in ("float", "double", "float32", "float64"):
            return None
        return Switch(Variable(var_name), cases)

    @register_emitter(If)
    def emit_if(self, node: If):
        sw = self._switchable_if(node)
        if sw is not None:
            self.emit_switch(sw)
            return

        # ---- Unreachable-branch pruning: condition folds to a constant ----
        cond_val = _const_int_value(node.cond)
        if (
            cond_val is None
            and isinstance(node.cond, Variable)
            and self._is_const_var(node.cond.name)
        ):
            cond_val = self._const_var_value(node.cond.name)
            if cond_val is not None:
                condensed = 1 if cond_val != 0 else 0
                cond_val = condensed
        if cond_val is not None:
            if cond_val != 0:
                self._push_scope()
                for stmt in node.body:
                    if not self._block_terminated():
                        self.emit(stmt)
                self._pop_scope()
            else:
                self._push_scope()
                for stmt in node.orelse or []:
                    if not self._block_terminated():
                        self.emit(stmt)
                self._pop_scope()
            return

        cond = self._truthy_expr(node.cond)
        then_bb = self.builder.append_basic_block("then")

        if node.orelse:
            else_bb = self.builder.append_basic_block("else")

        end_bb = self.builder.append_basic_block("endif")

        if node.orelse:
            self.builder.cbranch(cond, then_bb, else_bb)
        else:
            self.builder.cbranch(cond, then_bb, end_bb)

        self.builder.position_at_end(then_bb)
        self._push_scope()
        for stmt in node.body:
            if not self._block_terminated():
                self.emit(stmt)
        self._pop_scope()
        if not self._block_terminated():
            self.builder.branch(end_bb)

        if node.orelse:
            self.builder.position_at_end(else_bb)
            self._push_scope()
            for stmt in node.orelse:
                if not self._block_terminated():
                    self.emit(stmt)
            self._pop_scope()
            if not self._block_terminated():
                self.builder.branch(end_bb)

        self.builder.position_at_end(end_bb)

    def _promote(self, left, right):
        if isinstance(left.type, ir.DoubleType) and not isinstance(
            right.type, ir.DoubleType
        ):
            right = self.builder.sitofp(right, ir.DoubleType())
            return left, right
        if isinstance(right.type, ir.DoubleType) and not isinstance(
            left.type, ir.DoubleType
        ):
            left = self.builder.sitofp(left, ir.DoubleType())
            return left, right

        if isinstance(left.type, ir.PointerType) and isinstance(
            right.type, ir.PointerType
        ):
            return self.builder.ptrtoint(left, _i64), self.builder.ptrtoint(right, _i64)

        if isinstance(left.type, ir.PointerType) and isinstance(
            right.type, (ir.FloatType, ir.DoubleType)
        ):
            return self.builder.ptrtoint(left, _i64), self.builder.fptosi(right, _i64)
        if isinstance(right.type, ir.PointerType) and isinstance(
            left.type, (ir.FloatType, ir.DoubleType)
        ):
            return self.builder.fptosi(left, _i64), self.builder.ptrtoint(right, _i64)

        # Pointer + integer -> integer arithmetic on the pointer's address
        # (byte offset). char*/i8* pointers are included: string concatenation
        # is gated earlier by `_is_string_concat` (a `str` operand never reaches
        # _promote), so an i8* here is a genuine character/byte pointer.
        if isinstance(left.type, ir.PointerType) and isinstance(right.type, ir.IntType):  # type: ignore[attr-defined]
            left = self.builder.ptrtoint(left, _i64)
            right = self._promote_int(right, _i64)
            return left, right
        if isinstance(right.type, ir.PointerType) and isinstance(left.type, ir.IntType):  # type: ignore[attr-defined]
            right = self.builder.ptrtoint(right, _i64)
            left = self._promote_int(left, _i64)
            return left, right

        if isinstance(left.type, ir.IntType) and isinstance(right.type, ir.IntType):
            max_width = max(left.type.width, right.type.width)
            left = self._promote_int(left, ir.IntType(max_width))
            right = self._promote_int(right, ir.IntType(max_width))
        return left, right

    def _bitwise_promote(self, left, right):
        if isinstance(left.type, (ir.FloatType, ir.DoubleType)) or isinstance(
            right.type, (ir.FloatType, ir.DoubleType)
        ):
            src_ty = (
                left.type
                if isinstance(left.type, (ir.FloatType, ir.DoubleType))
                else right.type
            )
            int_ty = (
                ir.IntType(32) if isinstance(src_ty, ir.FloatType) else ir.IntType(64)
            )
            if isinstance(left.type, (ir.FloatType, ir.DoubleType)):
                left = self.builder.bitcast(left, int_ty)
            else:
                left = self._promote_int(left, int_ty)
            if isinstance(right.type, (ir.FloatType, ir.DoubleType)):
                right = self.builder.bitcast(right, int_ty)
            else:
                right = self._promote_int(right, int_ty)
        return left, right

    def _promote_int(self, val, target_ty):
        if val.type == target_ty:
            return val
        if isinstance(val.type, ir.IntType) and isinstance(target_ty, ir.IntType):
            if val.type.width == 1:
                return self.builder.zext(val, target_ty)
            if val.type.width < target_ty.width:
                return self.builder.sext(val, target_ty)
            if val.type.width > target_ty.width:
                return self.builder.trunc(val, target_ty)
        elif isinstance(val.type, (ir.FloatType, ir.DoubleType)) and isinstance(
            target_ty, ir.IntType
        ):
            return self.builder.fptosi(val, target_ty)
        return val

    def _is_true(self, val):
        if val.type == ir.IntType(1):
            return val
        if isinstance(val.type, ir.PointerType):
            return self.builder.icmp_unsigned("!=", val, ir.Constant(val.type, None))
        if isinstance(val.type, (ir.FloatType, ir.DoubleType)):
            return self.builder.fcmp_unordered("!=", val, ir.Constant(val.type, 0.0))
        return self.builder.icmp_signed("!=", val, ir.Constant(val.type, 0))

    # ------------------------------------------------------------------
    # Dynamic variable helpers
    # ------------------------------------------------------------------

    def _dyn_kind_for(self, ty):
        """Map a cpy type name to its runtime dynamic kind id."""
        ty = _bc_array_norm(ty)
        if ty == "dynamic":
            return _DYN_NONE
        if ty == "dynamic[]":
            return _DYN_LIST
        if ty == "int":
            return _DYN_INT
        if ty == "int64":
            return _DYN_INT64
        if ty == "uint64":
            return _DYN_UINT64
        if ty == "char":
            return _DYN_CHAR
        if ty == "bool":
            return _DYN_BOOL
        if ty in ("float", "double"):
            return _DYN_DOUBLE
        if ty == "str":
            return _DYN_STR
        if ty in ("big", "ubig"):
            return _DYN_BIG
        if ty.endswith(("*", "&")):
            return _DYN_PTR
        return _DYN_INT

    def _box_dyn(self, value, ty):
        """Box a runtime value into (kind, uint64 bits) for dynamic storage."""
        ty = _bc_array_norm(ty) or ty
        t = value.type
        if isinstance(t, ir.IntType):
            if t.width == 1:
                return _DYN_BOOL, self.builder.zext(value, _i64)
            if t.width == 8:
                return _DYN_CHAR, self.builder.zext(value, _i64)
            if t.width == 32:
                return _DYN_INT, self.builder.sext(value, _i64)
            if ty == "uint64":
                return _DYN_UINT64, value
            return _DYN_INT64, value
        if isinstance(t, (ir.FloatType, ir.DoubleType)):
            d = (
                value
                if isinstance(t, ir.DoubleType)
                else self.builder.fpext(value, _double)
            )
            return _DYN_DOUBLE, self.builder.bitcast(d, _i64)
        if isinstance(t, ir.PointerType):
            p = self.builder.ptrtoint(value, _i64)
            if ty == "dynamic[]":
                return _DYN_LIST, p
            if ty in ("big", "ubig"):
                return _DYN_BIG, p
            if isinstance(t.pointee, ir.IntType) and t.pointee.width == 8:  # type: ignore[attr-defined]
                return _DYN_STR, p
            return _DYN_PTR, p
        if ty == "uint64":
            return _DYN_UINT64, value
        return _DYN_INT64, value

    def _unbox_dyn(self, bits, ty):
        """Convert the i64 bits of a dynamic value back to the declared type."""
        if ty == "int":
            return self.builder.trunc(bits, _i32)
        if ty in ("int64", "uint64"):
            return bits
        if ty == "char":
            return self.builder.trunc(bits, _i8)
        if ty == "bool":
            return self.builder.trunc(bits, _i1)
        if ty in ("float", "double"):
            return self.builder.bitcast(bits, _double)
        if ty in ("str", "big", "void*"):
            return self.builder.inttoptr(bits, _i8ptr)
        if ty.endswith(("*", "&")):
            ptr_ty = self.llvm_type(ty)
            if isinstance(ptr_ty, ir.PointerType):
                return self.builder.inttoptr(bits, ptr_ty)
        return bits

    def _dyn_name(self, name):
        return self._string_const(name)

    def _is_dynamic_expr(self, node):
        """True when a node carries a runtime-typed (dynamic) value."""
        if getattr(node, "inferred_type", None) == "dynamic":
            return True
        return bool(isinstance(node, Variable) and getattr(node, "dynamic", False))

    def _dyn_local_ptr(self, name):
        """Return (creating if needed) the {i32,i64} slot that backs a dynamic local."""
        ptr = self.locals.get(name)
        if ptr is not None and getattr(ptr.type, "pointee", None) == _DynValue:
            return ptr
        ptr = self._alloca(_DynValue, name=f"dyn.{name}")
        self.locals[name] = ptr
        self.local_types[name] = "dynamic"
        return ptr

    def _dyn_struct(self, node):
        """Evaluate an expression and return it as a {i32,i64} DynValue struct."""
        if (
            self._is_decorator_factory
            and isinstance(node, Variable)
            and node.name == "result"
        ):
            return self.builder.load(self._code_result, "result")
        if isinstance(node, Variable) and getattr(node, "dynamic", False):
            ptr = self._dyn_local_ptr(node.name)
            return self.builder.load(ptr, node.name)
        val = self.emit(node)
        if val.type == _DynValue:
            return val
        kind, bits = self._box_dyn(val, getattr(node, "inferred_type", None) or "int")
        out = self.builder.insert_value(
            ir.Constant(_DynValue, ir.Undefined), ir.Constant(_i32, kind), 0
        )
        out = self.builder.insert_value(out, bits, 1)
        return out

    def _dyn_pair(self, node):
        """Return (kind: i32, bits: i64) for a dynamically-typed expression."""
        s = self._dyn_struct(node)
        k = self.builder.extract_value(s, 0)
        b = self.builder.extract_value(s, 1)
        return k, b

    def _unbox_dyn_to(self, node, want_ty):
        """Coerce a dynamically-typed expression to a concrete declared type."""
        k, b = self._dyn_pair(node)
        want = self._dyn_kind_for(want_ty)
        fn = self.functions.get("dyn_as_v")
        bits = self.builder.call(fn, [k, b, ir.Constant(_i32, want)])
        return self._unbox_dyn(bits, want_ty)

    def _emit_dyn_load(self, node):
        name = node.name
        ty = getattr(node, "inferred_type", None) or "int"
        if ty == "dynamic":
            return self._dyn_struct(node)
        ptr = self.locals.get(name)
        if ptr is not None and getattr(ptr.type, "pointee", None) == _DynValue:
            return self._unbox_dyn_to(node, ty)
        kind = self._dyn_kind_for(ty)
        fn = self.functions.get("dyn_as")
        bits = self.builder.call(fn, [self._dyn_name(name), ir.Constant(_i32, kind)])
        return self._unbox_dyn(bits, ty)

    def _emit_dyn_store(self, name, value, value_ty):
        if value.type == _DynValue:
            self.builder.store(value, self._dyn_local_ptr(name))
        else:
            kind, bits = self._box_dyn(value, value_ty or "int")
            out = self.builder.insert_value(
                ir.Constant(_DynValue, ir.Undefined), ir.Constant(_i32, kind), 0
            )
            out = self.builder.insert_value(out, bits, 1)
            self.builder.store(out, self._dyn_local_ptr(name))
        self.ssa_values.pop(name, None)
        self.ssa_types.pop(name, None)

    def _truthy_expr(self, node):
        """Evaluate an expression to an i1 truth value, dispatching dynamic vars."""
        if not self.no_userspace and self._is_dynamic_expr(node):
            k, b = self._dyn_pair(node)
            fn = self.functions.get("dyn_truthy_v")
            c = self.builder.call(fn, [k, b])
            return self.builder.trunc(c, _i1)
        if (
            isinstance(node, Variable)
            and getattr(node, "dynamic", False)
            and not self.no_userspace
        ):
            name_ptr = self._dyn_name(node.name)
            c = self.builder.call(self.functions.get("dyn_truthy"), [name_ptr])
            return self.builder.trunc(c, _i1)
        val = self.emit(node)
        if val.type != ir.IntType(1):
            val = self._is_true(val)
        return val

    def _block_terminated(self) -> bool:
        block = self.builder.block
        if block is None:
            return True
        return block.is_terminated

    # ------------------------------------------------------------------
    # cpdb debug instrumentation (opt-in)
    # ------------------------------------------------------------------

    def _dbg_node_gv(self):
        if self._dbg_node_global is None:
            gv = ir.GlobalVariable(self.module, _i32, name="cpdbd_node")
            gv.initializer = ir.Constant(_i32, 0)
            gv.align = 4
            self._dbg_node_global = gv
        return self._dbg_node_global

    def _dbg_tracer(self, nid: int):
        fn = self._dbg_tracer_fns.get(nid)
        if fn is not None:
            return fn
        fn = ir.Function(self.module, ir.FunctionType(_void, []), name=f"cpdbd_t_{nid}")
        entry = fn.append_basic_block("entry")
        ir.IRBuilder(entry).ret_void()
        self._dbg_tracer_fns[nid] = fn
        return fn

    def _dbg_precollect(self, name: str, params: dict, body: list) -> list[str]:
        """Collect unique local names (params first, then DFS over statements)
        so the per-function debug box can be sized up front."""
        if name in self._dbg_collect:
            return self._dbg_collect[name]
        names: list[str] = []
        seen: set[str] = set()
        for p in params:
            if p not in seen:
                seen.add(p)
                names.append(p)
        stack = list(body)
        while stack:
            stmt = stack.pop()
            if stmt is None or isinstance(stmt, str):
                continue
            if isinstance(stmt, dict):
                if stmt.get("type") == "for":
                    v = stmt.get("var")
                    if isinstance(v, str) and v not in seen:
                        seen.add(v)
                        names.append(v)
                    stack.extend(stmt.get("body") or [])
                    continue
                for _k, _v in stmt.items():
                    if isinstance(_v, list):
                        stack.extend(_v)
                continue
            if isinstance(stmt, VarDecl):
                n = getattr(stmt, "name", None)
                if isinstance(n, str) and n not in seen:
                    seen.add(n)
                    names.append(n)
            elif isinstance(stmt, Assign):
                t = stmt.target
                if isinstance(t, str) or isinstance(t, Variable):
                    n = t if isinstance(t, str) else getattr(t, "name", None)
                    if isinstance(n, str) and n not in seen:
                        seen.add(n)
                        names.append(n)
            for kid in self._dbg_body_kids(stmt):
                stack.append(kid)
        self._dbg_collect[name] = names
        return names

    def _dbg_body_kids(self, stmt) -> list:
        kids: list = []
        for attr in ("body", "orelse"):
            v = getattr(stmt, attr, None)
            if v:
                kids.extend(v)
        if isinstance(stmt, (Try,)):
            kids.extend(list(getattr(stmt, "body", None) or []))
            for h in getattr(stmt, "handlers", None) or []:
                kids.extend(getattr(h, "body", None) or [])
            return kids
        if isinstance(stmt, (Switch,)):
            for _val, body in getattr(stmt, "cases", []) or []:
                kids.extend(list(body))
            return kids
        if isinstance(stmt, (DeferStmt,)):
            inner = getattr(stmt, "body", None)
            if inner is not None:
                kids.append(inner)
            return kids
        return kids

    def _dbg_begin_function(self, name: str, params: dict, body: list):
        """(Re)target subsequent tracer refreshes at `name`'s debug box."""
        if not self.debug_instrument:
            return
        self._dbg_cur_fn = name
        names = self._dbg_precollect(name, params, body)
        if name in self._dbg_box_slots:
            return
        slots = {n: i for i, n in enumerate(names)}
        self._dbg_box_slots[name] = slots
        k = max(len(slots) + 8, 16)
        bty = ir.ArrayType(_i8ptr, k)
        box = ir.GlobalVariable(self.module, bty, name=f"cpdbd_box_{name}")
        box.initializer = ir.Constant(bty, ir.Undefined)
        box.align = 8
        self._dbg_boxes[name] = box

    def _dbg_emit_tracer(self, node, nid: int):
        """Store the entering node id, refresh the current function's debug
        box, then call the `cpdbd_t_<nid>` tracer (a breakable symbol)."""
        if not self.debug_instrument:
            return
        try:
            if self.builder is None or self.builder.block.is_terminated:
                return
        except Exception:
            return
        block = self.builder.block
        key = (nid, block)
        if key in self._dbg_emitted:
            return
        self._dbg_emitted.add(key)
        self.builder.store(ir.Constant(_i32, nid), self._dbg_node_gv())
        name = self._dbg_cur_fn
        slots = self._dbg_box_slots.get(name)
        if slots and name in self._dbg_boxes:
            box = self._dbg_boxes[name]
            i8p = ir.PointerType(_i8)
            for var, slot in slots.items():
                ptr = self.locals.get(var)
                if ptr is None:
                    continue
                ty = self.local_types.get(var)
                if ty:
                    self._dbg_box_types.setdefault(name, {})[var] = ty
                gep = self.builder.gep(
                    box, [ir.Constant(_i32, 0), ir.Constant(_i32, slot)]
                )
                self.builder.store(self.builder.bitcast(ptr, i8p), gep)
        self.builder.call(self._dbg_tracer(nid), [])

    def _dbg_loop_head(self, node):
        """Emit the loop-head tracer (each iteration) for loops."""
        if not self.debug_instrument:
            return
        nid = self.debug_nids.get(id(node))
        if nid is None:
            return
        self._dbg_emit_tracer(node, nid)

    def _run_deferred(self):
        """Emit all accumulated deferred statements in LIFO order.

        Called at every function exit point (explicit `return` statements and the
        implicit end-of-function return). It does NOT clear the accumulated list,
        so every static exit site of the function emits the full set of defers;
        at runtime only the exit that is actually reached executes them. The list
        is reset when the function's own emission completes (emit_funcdef restores
        the outer value).
        """
        if not self._deferred:
            return
        for stmt in reversed(self._deferred):
            if self._block_terminated():
                break
            self.emit(stmt)
            if self._block_terminated():
                break

    # ------------------------------------------------------------------
    # 4.  Algebraic-identity folding (called at the top of emit_binop)
    # ------------------------------------------------------------------

    def _try_algebraic_simplify(self, node: BinOp):
        """Attempt to fold algebraic identities *before* emitting IR.

        Returns an ``ir.Value`` when the simplification succeeded, else *None*.
        big/ubig/str/dynamic operands are left to the normal path.  Integer
        operands get the classic identities; float/double operands get exact
        IEEE-safe folds only (``x*1.0 -> x``, ``x/-1.0 -> -x``, ``x-0.0 -> x``,
        ``pow(x,{0,1,2,0.5,-1}) -> 1/x/x*x/sqrt(x)/1-x``).
        """
        op = node.op
        left_node = node.left
        right_node = node.right

        # Only simplify when both sides are simple enough to emit cheaply.
        lt = getattr(left_node, "inferred_type", "")
        rt = getattr(right_node, "inferred_type", "")
        if lt in ("big", "ubig", "str", "dynamic") or rt in (
            "big",
            "ubig",
            "str",
            "dynamic",
        ):
            return None

        # ---- Constant propagation: substitute tracked const vars ----
        # Remember the declared type width of a substituted var: the literal
        # `Number` the const is lowered to would otherwise lose a 64-bit
        # declared type (int64/uint64), folding in 32 bits and truncating.
        left_width = 0
        right_width = 0
        left_decl = None
        right_decl = None
        if isinstance(left_node, Variable) and self._is_const_var(left_node.name):
            cv = self._const_var_value(left_node.name)
            if cv is not None and left_node.name in self.locals:
                left_decl = self.local_types.get(left_node.name, "int")
                left_node = Number(str(cv))
                left_width = 64 if left_decl in ("int64", "uint64") else 32
        if isinstance(right_node, Variable) and self._is_const_var(right_node.name):
            cv = self._const_var_value(right_node.name)
            if cv is not None and right_node.name in self.locals:
                right_decl = self.local_types.get(right_node.name, "int")
                right_node = Number(str(cv))
                right_width = 64 if right_decl in ("int64", "uint64") else 32
        if isinstance(left_node, Variable) and left_node.name in self._const_prop_f:
            fv = self._const_prop_f.get(left_node.name)
            if fv is not None and left_node.name in self.locals:
                left_node = Number(repr(fv))
                lt = "double"
        if isinstance(right_node, Variable) and right_node.name in self._const_prop_f:
            fv = self._const_prop_f.get(right_node.name)
            if fv is not None and right_node.name in self.locals:
                right_node = Number(repr(fv))
                rt = "double"

        lf = _const_fp_value(left_node)
        rf = _const_fp_value(right_node)
        is_fp = (
            lt in ("float", "double")
            or rt in ("float", "double")
            or lf is not None
            or rf is not None
        )

        # ---- Constant folding: both operands are integral literals ----
        lv = _const_int_value(left_node)
        rv = _const_int_value(right_node)
        if lv is not None and rv is not None:
            folded = _fold_int_binop(op, lv, rv)
            if folded is not None:
                lval = self.emit(left_node)
                if isinstance(lval.type, ir.IntType):
                    width = lval.type.width
                else:
                    width = 32
                width = max(width, left_width, right_width)
                v32 = _trunc_to_signed(folded, width)
                return ir.Constant(ir.IntType(width), v32)

        # ---- POW strength reduction: x**0/1/2/3 -> exact arithmetic ----
        if op == TokenType.POW:
            exp = rf if rf is not None else _const_int_value(right_node)
            if exp is not None:
                lval = self.emit(left_node)
                if isinstance(exp, int) and isinstance(lval.type, ir.IntType):
                    # The literal emitted for a substituted int64/uint64 const
                    # would be i32; widen to the declared variable width so the
                    # `**{0,1,2,3}` strength reduction does not truncate.
                    pw = max(lval.type.width, left_width)
                    if pw > lval.type.width:
                        lval = self._extend_to_i64(lval)
                    if exp == 0:
                        return ir.Constant(lval.type, 1)
                    if exp == 1:
                        return lval
                    if exp == 2:
                        return self.builder.mul(lval, lval)
                    if exp == 3:
                        return self.builder.mul(self.builder.mul(lval, lval), lval)
                if isinstance(lval.type, ir.DoubleType):
                    e = float(exp)
                    if e == 0.0:
                        return ir.Constant(lval.type, 1.0)
                    if e == 1.0:
                        return lval
                    if e == 2.0:
                        if lf is not None:
                            return ir.Constant(lval.type, lf * lf)
                        return self.builder.fmul(lval, lval)
                    if e == 0.5:
                        return self._emit_sqrt_f64(lval)
                    if e == -1.0:
                        return self.builder.fdiv(ir.Constant(lval.type, 1.0), lval)

        # ---- Floating-point constant folding + exact identities ----
        if is_fp:
            if (
                lf is not None
                and rf is not None
                and op
                in (
                    TokenType.STAR,
                    TokenType.SLASH,
                    TokenType.PLUS,
                    TokenType.MINUS,
                )
            ):
                if not (op == TokenType.SLASH and rf == 0.0):
                    lval = self.emit(left_node)
                    rval = self.emit(right_node)
                    lval, rval = self._promote(lval, rval)
                    if isinstance(lval.type, ir.DoubleType):
                        if op == TokenType.STAR:
                            v = lf * rf
                        elif op == TokenType.SLASH:
                            v = lf / rf
                        elif op == TokenType.PLUS:
                            v = lf + rf
                        else:
                            v = lf - rf
                        return ir.Constant(lval.type, v)
            if op == TokenType.STAR:
                if rf is not None and rf == 1.0:
                    return self.emit(left_node)
                if rf is not None and rf == -1.0:
                    return self._emit_fneg(self.emit(left_node))
                if lf is not None and lf == 1.0:
                    return self.emit(right_node)
                if lf is not None and lf == -1.0:
                    return self._emit_fneg(self.emit(right_node))
            if op in (TokenType.SLASH, TokenType.SLASH_SLASH):
                if rf is not None and rf == 1.0:
                    return self.emit(left_node)
                if rf is not None and rf == -1.0:
                    return self._emit_fneg(self.emit(left_node))
            if op == TokenType.MINUS:
                if rf is not None and rf == 0.0:
                    return self.emit(left_node)
            # No exact identity for this float op; leave to emit_binop.
            return None

        # ---- x * 0  /  0 * x  ->  0 ----

        # Only simplify when both sides are simple enough to emit cheaply
        # and the result type is integral (avoid messing with floats/pointers).

        # ---- x * 0  /  0 * x  ->  0 ----
        if op == TokenType.STAR:
            if _is_const_zero(left_node) or _is_const_zero(right_node):
                left_val = self.emit(left_node)
                if isinstance(left_val.type, ir.IntType):
                    return ir.Constant(left_val.type, 0)
            # ---- x * 1  /  1 * x  ->  x ----
            if _is_const_one(left_node):
                return self.emit(right_node)
            if _is_const_one(right_node):
                return self.emit(left_node)
            # ---- x * 2^k  ->  x << k ----
            shift_k = _detect_shift_amount_from_mul(node)
            if shift_k is not None:
                val = self.emit(left_node)
                other = right_node
                # Ensure val is the variable / expression, other is the constant
                if (
                    _const_int_value(left_node) is not None
                    and _const_int_value(left_node) == 2**shift_k
                ):
                    val = self.emit(right_node)
                    other = left_node
                if isinstance(val.type, ir.IntType) and val.type.width >= 2:
                    k = ir.Constant(val.type, shift_k)
                    k = self._clamp_shift_amount(k, val.type.width)
                    return self.builder.shl(val, k)
            # ---- x * (2^k + 1)  ->  (x << k) + x ----
            left_v = _const_int_value(left_node)
            right_v = _const_int_value(right_node)
            if left_v is not None and left_v > 2 and _is_power_of_2(left_v - 1):
                # right_node * (left_v)  ->  (right << k) + right
                k = _log2_floor(left_v - 1)
                val = self.emit(right_node)
                if isinstance(val.type, ir.IntType):
                    shifted = self.builder.shl(val, ir.Constant(val.type, k))
                    return self.builder.add(shifted, val)
            if right_v is not None and right_v > 2 and _is_power_of_2(right_v - 1):
                k = _log2_floor(right_v - 1)
                val = self.emit(left_node)
                if isinstance(val.type, ir.IntType):
                    shifted = self.builder.shl(val, ir.Constant(val.type, k))
                    return self.builder.add(shifted, val)
            # ---- x * (2^k - 1)  ->  (x << k) - x ----
            if left_v is not None and left_v > 1 and _is_power_of_2(left_v + 1):
                k = _log2_floor(left_v + 1)
                val = self.emit(right_node)
                if isinstance(val.type, ir.IntType):
                    shifted = self.builder.shl(val, ir.Constant(val.type, k))
                    return self.builder.sub(shifted, val)
            if right_v is not None and right_v > 1 and _is_power_of_2(right_v + 1):
                k = _log2_floor(right_v + 1)
                val = self.emit(left_node)
                if isinstance(val.type, ir.IntType):
                    shifted = self.builder.shl(val, ir.Constant(val.type, k))
                    return self.builder.sub(shifted, val)

        # ---- x + 0  /  0 + x  ->  x ----
        if op == TokenType.PLUS:
            if _is_const_zero(left_node):
                return self.emit(right_node)
            if _is_const_zero(right_node):
                return self.emit(left_node)

        # ---- x - 0  ->  x ----
        if op == TokenType.MINUS:
            if _is_const_zero(right_node):
                return self.emit(left_node)
            if (
                isinstance(left_node, Variable)
                and isinstance(right_node, Variable)
                and left_node.name == right_node.name
            ):
                val = self.emit(left_node)
                if isinstance(val.type, ir.IntType):
                    return ir.Constant(val.type, 0)

        # ---- x ^ x  ->  0 ----
        if op == TokenType.CARET:
            if _is_const_zero(right_node):
                return self.emit(left_node)
            if _is_const_zero(left_node):
                return self.emit(right_node)
            if (
                isinstance(left_node, Variable)
                and isinstance(right_node, Variable)
                and left_node.name == right_node.name
            ):
                val = self.emit(left_node)
                if isinstance(val.type, ir.IntType):
                    return ir.Constant(val.type, 0)

        # ---- (x << c1) << c2  ->  x << (c1+c2) ----
        if op == TokenType.SHL:
            if isinstance(left_node, BinOp) and left_node.op == TokenType.SHL:
                if isinstance(left_node.right, Number) and isinstance(
                    right_node, Number
                ):
                    try:
                        c1 = int(left_node.right.value)
                        c2 = int(right_node.value)
                        total = c1 + c2
                        val = self.emit(left_node.left)
                        if isinstance(val.type, ir.IntType):
                            k = ir.Constant(val.type, total)
                            k = self._clamp_shift_amount(k, val.type.width)
                            return self.builder.shl(val, k)
                    except (ValueError, TypeError):
                        pass

        # ---- x / 1  ->  x ----
        if op in (TokenType.SLASH, TokenType.SLASH_SLASH):
            if _is_const_one(right_node):
                return self.emit(left_node)

        # ---- x % 1  ->  0 ----
        if op == TokenType.PERCENT:
            if _is_const_one(right_node):
                left_val = self.emit(left_node)
                return ir.Constant(left_val.type, 0)

        # ---- x & 0  ->  0 ----
        if op == TokenType.AMPERSAND:
            if _is_const_zero(right_node):
                left_val = self.emit(left_node)
                if isinstance(left_val.type, ir.IntType):
                    return ir.Constant(left_val.type, 0)
            if _is_const_zero(left_node):
                right_val = self.emit(right_node)
                if isinstance(right_val.type, ir.IntType):
                    return ir.Constant(right_val.type, 0)
            if (
                isinstance(left_node, Variable)
                and isinstance(right_node, Variable)
                and left_node.name == right_node.name
            ):
                val = self.emit(left_node)
                if isinstance(val.type, ir.IntType):
                    return val

        # ---- x | 0  ->  x ----
        if op == TokenType.PIPE:
            if _is_const_zero(right_node):
                return self.emit(left_node)
            if _is_const_zero(left_node):
                return self.emit(right_node)

        # ---- strength reduction: x * 3/5/9/10  ->  shifts + adds ----
        if op == TokenType.STAR:
            for var_node, const_node in (
                (left_node, right_node),
                (right_node, left_node),
            ):
                if isinstance(var_node, Variable) and isinstance(const_node, Number):
                    cv = _const_int_value(const_node)
                    if cv == 3:
                        return self._strength_reduce_mul3(var_node)
                    if cv == 5:
                        return self._strength_reduce_mul5(var_node)
                    if cv == 9:
                        return self._strength_reduce_mul9(var_node)
                    if cv == 10:
                        return self._strength_reduce_mul10(var_node)

        return None

    def _strength_reduce_mul3(self, var_node):
        val = self.emit(var_node)
        t = val.type
        shifted = self.builder.shl(val, ir.Constant(t, 1))
        return self.builder.add(shifted, val)

    def _strength_reduce_mul5(self, var_node):
        val = self.emit(var_node)
        t = val.type
        shifted = self.builder.shl(val, ir.Constant(t, 2))
        return self.builder.add(shifted, val)

    def _strength_reduce_mul9(self, var_node):
        val = self.emit(var_node)
        t = val.type
        shifted = self.builder.shl(val, ir.Constant(t, 3))
        return self.builder.add(shifted, val)

    def _strength_reduce_mul10(self, var_node):
        val = self.emit(var_node)
        t = val.type
        s3 = self.builder.shl(val, ir.Constant(t, 3))
        s1 = self.builder.shl(val, ir.Constant(t, 1))
        return self.builder.add(s3, s1)

    @register_emitter(BinOp)
    def emit_binop(self, node):
        # ---- 4. Algebraic-identity folding ----
        simplified = self._try_algebraic_simplify(node)
        if simplified is not None:
            return simplified

        if node.op == TokenType.PLUS and self._is_string_concat(node):
            return self._emit_string_concat(node)

        # Runtime dispatch when either operand is dynamically typed.
        if (
            not self.no_userspace
            and (self._is_dynamic_expr(node.left) or self._is_dynamic_expr(node.right))
            and node.op
            in (
                TokenType.PLUS,
                TokenType.MINUS,
                TokenType.STAR,
                TokenType.SLASH,
                TokenType.SLASH_SLASH,
                TokenType.PERCENT,
                TokenType.EQ_EQ,
                TokenType.NOT_EQ,
                TokenType.LESS,
                TokenType.GREATER,
                TokenType.LESS_EQ,
                TokenType.GREATER_EQ,
                TokenType.AMPERSAND,
                TokenType.PIPE,
                TokenType.CARET,
                TokenType.SHL,
                TokenType.SHR,
            )
        ):
            return self._emit_dyn_binop(node)

        match node.op:
            case TokenType.AND:
                lhs_true = self._truthy_expr(node.left)
                entry_bb = self.builder.block
                rhs_bb = self.builder.append_basic_block("and.rhs")
                end_bb = self.builder.append_basic_block("and.end")
                self.builder.cbranch(lhs_true, rhs_bb, end_bb)
                self.builder.position_at_end(rhs_bb)
                rhs_true = self._truthy_expr(node.right)
                actual_rhs_bb = self.builder.block
                self.builder.branch(end_bb)
                self.builder.position_at_end(end_bb)
                phi = self.builder.phi(_i1)
                phi.add_incoming(ir.Constant(_i1, 0), entry_bb)
                phi.add_incoming(rhs_true, actual_rhs_bb)
                return phi

            case TokenType.OR:
                lhs_true = self._truthy_expr(node.left)
                entry_bb = self.builder.block
                rhs_bb = self.builder.append_basic_block("or.rhs")
                end_bb = self.builder.append_basic_block("or.end")
                self.builder.cbranch(lhs_true, end_bb, rhs_bb)
                self.builder.position_at_end(rhs_bb)
                rhs_true = self._truthy_expr(node.right)
                actual_rhs_bb = self.builder.block
                self.builder.branch(end_bb)
                self.builder.position_at_end(end_bb)
                phi = self.builder.phi(_i1)
                phi.add_incoming(ir.Constant(_i1, 1), entry_bb)
                phi.add_incoming(rhs_true, actual_rhs_bb)
                return phi

        left = self.emit(node.left)
        right = self.emit(node.right)

        # `in` — dynamic-list membership (node.op is TokenType.KEYWORD only for
        # the `in` infix operator). `x in list`: left=needle, right=list.
        if node.op == TokenType.KEYWORD:
            list_bits = (
                right
                if isinstance(right.type, ir.IntType)
                else self.builder.ptrtoint(
                    self.builder.bitcast(right, _DynValuePtr), _i64
                )
            )
            kind, bits = self._box_dyn(
                left, getattr(node.left, "inferred_type", None) or "int"
            )
            return self.builder.call(
                self.functions["dyn_list_contains"],
                [list_bits, ir.Constant(_i32, kind), bits],
            )

        # `list * int` — dynamic list repetition (e.g. `[false] * n`).
        if (
            node.op == TokenType.STAR
            and getattr(node.left, "inferred_type", None) == "dynamic[]"
        ):
            list_bits = self.builder.ptrtoint(
                self.builder.bitcast(left, _DynValuePtr), _i64
            )
            n = (
                self._extend_to_i64(right)
                if isinstance(right.type, ir.IntType)
                else right
            )
            if isinstance(n.type, (ir.DoubleType, ir.FloatType)):
                n = self.builder.fptosi(n, _i64)
            return self.builder.call(self.functions["dyn_list_repeat"], [list_bits, n])

        # ubig (unsigned big) arithmetic + bitwise before _promote. add/mul/div/

        # mod/pow are magnitude ops (identical to signed for non-negative
        # operands), so bigint_* is reused; subtraction is underflow-checked and
        # comparisons are unsigned via ubigint_*. Bitwise ops (and/or/xor/shift)
        # work over the full magnitude and are unsupported on signed `big`.
        if self._is_ubig(node.left) or self._is_ubig(node.right):
            if not isinstance(left.type, ir.PointerType):
                left = self._promote_to_ubig(left)
            if not isinstance(right.type, ir.PointerType):
                right = self._promote_to_ubig(right)
            match node.op:
                case TokenType.PLUS:
                    return self.builder.call(
                        self.functions["bigint_add"], [left, right]
                    )
                case TokenType.MINUS:
                    return self.builder.call(
                        self.functions["ubigint_sub"], [left, right]
                    )
                case TokenType.STAR:
                    return self.builder.call(
                        self.functions["bigint_mul"], [left, right]
                    )
                case TokenType.SLASH:
                    return self.builder.call(
                        self.functions["bigint_div"], [left, right]
                    )
                case TokenType.SLASH_SLASH:
                    return self.builder.call(
                        self.functions["bigint_floor_div"], [left, right]
                    )
                case TokenType.PERCENT:
                    return self.builder.call(
                        self.functions["bigint_mod"], [left, right]
                    )
                case TokenType.POW:
                    return self.builder.call(
                        self.functions["bigint_pow"], [left, right]
                    )
                case TokenType.AMPERSAND:
                    return self.builder.call(
                        self.functions["ubigint_and"], [left, right]
                    )
                case TokenType.PIPE:
                    return self.builder.call(
                        self.functions["ubigint_or"], [left, right]
                    )
                case TokenType.CARET:
                    return self.builder.call(
                        self.functions["ubigint_xor"], [left, right]
                    )
                case TokenType.SHL:
                    return self.builder.call(
                        self.functions["ubigint_shl"], [left, right]
                    )
                case TokenType.SHR:
                    return self.builder.call(
                        self.functions["ubigint_shr"], [left, right]
                    )
                case TokenType.EQ_EQ:
                    cmp = self.builder.call(
                        self.functions["ubigint_cmp"], [left, right]
                    )
                    return self.builder.icmp_signed("==", cmp, ir.Constant(_i32, 0))
                case TokenType.NOT_EQ:
                    cmp = self.builder.call(
                        self.functions["ubigint_cmp"], [left, right]
                    )
                    return self.builder.icmp_signed("!=", cmp, ir.Constant(_i32, 0))
                case TokenType.LESS:
                    cmp = self.builder.call(
                        self.functions["ubigint_cmp"], [left, right]
                    )
                    return self.builder.icmp_signed("<", cmp, ir.Constant(_i32, 0))
                case TokenType.GREATER:
                    cmp = self.builder.call(
                        self.functions["ubigint_cmp"], [left, right]
                    )
                    return self.builder.icmp_signed(">", cmp, ir.Constant(_i32, 0))
                case TokenType.LESS_EQ:
                    cmp = self.builder.call(
                        self.functions["ubigint_cmp"], [left, right]
                    )
                    return self.builder.icmp_signed("<=", cmp, ir.Constant(_i32, 0))
                case TokenType.GREATER_EQ:
                    cmp = self.builder.call(
                        self.functions["ubigint_cmp"], [left, right]
                    )
                    return self.builder.icmp_signed(">=", cmp, ir.Constant(_i32, 0))

        # Handle big arithmetic before _promote (which would corrupt i8* big values)
        if self._is_big(node.left) or self._is_big(node.right):
            if not isinstance(left.type, ir.PointerType):
                left = self._promote_to_big(
                    left, getattr(node.left, "inferred_type", None)
                )
            if not isinstance(right.type, ir.PointerType):
                right = self._promote_to_big(
                    right, getattr(node.right, "inferred_type", None)
                )
            match node.op:
                case TokenType.PLUS:
                    return self.builder.call(
                        self.functions["bigint_add"], [left, right]
                    )
                case TokenType.MINUS:
                    return self.builder.call(
                        self.functions["bigint_sub"], [left, right]
                    )
                case TokenType.STAR:
                    return self.builder.call(
                        self.functions["bigint_mul"], [left, right]
                    )
                case TokenType.SLASH:
                    return self.builder.call(
                        self.functions["bigint_div"], [left, right]
                    )
                case TokenType.SLASH_SLASH:
                    return self.builder.call(
                        self.functions["bigint_floor_div"], [left, right]
                    )
                case TokenType.PERCENT:
                    return self.builder.call(
                        self.functions["bigint_mod"], [left, right]
                    )
                case TokenType.POW:
                    return self.builder.call(
                        self.functions["bigint_pow"], [left, right]
                    )
                case TokenType.EQ_EQ:
                    cmp = self.builder.call(self.functions["bigint_cmp"], [left, right])
                    return self.builder.icmp_signed("==", cmp, ir.Constant(_i32, 0))
                case TokenType.NOT_EQ:
                    cmp = self.builder.call(self.functions["bigint_cmp"], [left, right])
                    return self.builder.icmp_signed("!=", cmp, ir.Constant(_i32, 0))
                case TokenType.LESS:
                    cmp = self.builder.call(self.functions["bigint_cmp"], [left, right])
                    return self.builder.icmp_signed("<", cmp, ir.Constant(_i32, 0))
                case TokenType.GREATER:
                    cmp = self.builder.call(self.functions["bigint_cmp"], [left, right])
                    return self.builder.icmp_signed(">", cmp, ir.Constant(_i32, 0))
                case TokenType.LESS_EQ:
                    cmp = self.builder.call(self.functions["bigint_cmp"], [left, right])
                    return self.builder.icmp_signed("<=", cmp, ir.Constant(_i32, 0))
                case TokenType.GREATER_EQ:
                    cmp = self.builder.call(self.functions["bigint_cmp"], [left, right])
                    return self.builder.icmp_signed(">=", cmp, ir.Constant(_i32, 0))

        # String comparison via strcmp (content, not pointer comparison)
        left_str = getattr(node.left, "inferred_type", None) == "str"
        right_str = getattr(node.right, "inferred_type", None) == "str"
        if left_str and right_str:
            cmp = self.builder.call(self._strcmp_fn, [left, right])
            zero = ir.Constant(_i32, 0)
            match node.op:
                case TokenType.EQ_EQ:
                    return self.builder.icmp_signed("==", cmp, zero)
                case TokenType.NOT_EQ:
                    return self.builder.icmp_signed("!=", cmp, zero)
                case TokenType.LESS:
                    return self.builder.icmp_signed("<", cmp, zero)
                case TokenType.GREATER:
                    return self.builder.icmp_signed(">", cmp, zero)
                case TokenType.LESS_EQ:
                    return self.builder.icmp_signed("<=", cmp, zero)
                case TokenType.GREATER_EQ:
                    return self.builder.icmp_signed(">=", cmp, zero)

        left, right = self._promote(left, right)
        is_float = isinstance(left.type, ir.DoubleType) or isinstance(
            right.type, ir.DoubleType
        )
        if is_float:
            match node.op:
                case TokenType.PLUS:
                    return self.builder.fadd(left, right)
                case TokenType.MINUS:
                    return self.builder.fsub(left, right)
                case TokenType.STAR:
                    return self.builder.fmul(left, right)
                case TokenType.SLASH:
                    return self.builder.fdiv(left, right)
                case TokenType.SLASH_SLASH:
                    div = self.builder.fdiv(left, right)
                    return self.builder.call(self._get_floor_fn(), [div])
                case TokenType.PERCENT:
                    return self.builder.frem(left, right)
                case TokenType.GREATER:
                    return self.builder.fcmp_ordered(">", left, right)
                case TokenType.LESS:
                    return self.builder.fcmp_ordered("<", left, right)
                case TokenType.GREATER_EQ:
                    return self.builder.fcmp_ordered(">=", left, right)
                case TokenType.LESS_EQ:
                    return self.builder.fcmp_ordered("<=", left, right)
                case TokenType.EQ_EQ:
                    return self.builder.fcmp_ordered("==", left, right)
                case TokenType.NOT_EQ:
                    return self.builder.fcmp_ordered("!=", left, right)

        match node.op:
            case TokenType.PLUS:
                return self.builder.add(left, right)
            case TokenType.MINUS:
                return self.builder.sub(left, right)
            case TokenType.STAR:
                return self.builder.mul(left, right)
            case TokenType.SLASH:
                return self._emit_int_divmod(left, right, is_rem=False)
            case TokenType.SLASH_SLASH:
                return self._emit_floor_div(left, right)
            case TokenType.PERCENT:
                return self._emit_int_divmod(left, right, is_rem=True)
            case TokenType.SHL:
                left, right = self._bitwise_promote(left, right)
                bitwidth = left.type.width
                right = self._clamp_shift_amount(right, bitwidth)
                return self.builder.shl(left, right)
            case TokenType.SHR:
                left, right = self._bitwise_promote(left, right)
                bitwidth = left.type.width
                right = self._clamp_shift_amount(right, bitwidth)
                return self.builder.ashr(left, right)
            case TokenType.AMPERSAND:
                left, right = self._bitwise_promote(left, right)
                return self.builder.and_(left, right)
            case TokenType.PIPE:
                left, right = self._bitwise_promote(left, right)
                return self.builder.or_(left, right)
            case TokenType.CARET:
                left, right = self._bitwise_promote(left, right)
                return self.builder.xor(left, right)
            case TokenType.GREATER:
                left, right = self._normalize_ptr_cmp(left, right)
                return self.builder.icmp_signed(">", left, right)
            case TokenType.LESS:
                left, right = self._normalize_ptr_cmp(left, right)
                return self.builder.icmp_signed("<", left, right)
            case TokenType.GREATER_EQ:
                left, right = self._normalize_ptr_cmp(left, right)
                return self.builder.icmp_signed(">=", left, right)
            case TokenType.LESS_EQ:
                left, right = self._normalize_ptr_cmp(left, right)
                return self.builder.icmp_signed("<=", left, right)
            case TokenType.EQ_EQ:
                left, right = self._normalize_ptr_cmp(left, right)
                return self.builder.icmp_signed("==", left, right)
            case TokenType.NOT_EQ:
                left, right = self._normalize_ptr_cmp(left, right)
                return self.builder.icmp_signed("!=", left, right)
            case TokenType.POW:
                return self._emit_pow(left, right)

    def _emit_fneg(self, val):
        """Exact IEEE negation (equivalent to XOR of the sign bit): multiply by
        ``-1.0``.  ``fsub(0.0, x)`` would return ``+0.0`` for ``x = +0.0``, so
        it is not exact."""
        if isinstance(val.type, ir.DoubleType):
            return self.builder.fmul(val, ir.Constant(ir.DoubleType(), -1.0))
        if isinstance(val.type, ir.FloatType):
            return self.builder.fmul(val, ir.Constant(ir.FloatType(), -1.0))
        return val

    def _emit_sqrt_f64(self, val):
        fn = self.functions.get("llvm.sqrt.f64")
        if fn is None:
            fn_ty = ir.FunctionType(ir.DoubleType(), [ir.DoubleType()])
            fn = ir.Function(self.module, fn_ty, name="llvm.sqrt.f64")
            self.functions["llvm.sqrt.f64"] = fn
        return self.builder.call(fn, [val])

    def _emit_pow(self, left, right):
        pow_func = self.functions.get("pow")
        if pow_func is None:
            pow_ty = ir.FunctionType(
                ir.DoubleType(), [ir.DoubleType(), ir.DoubleType()]
            )
            pow_func = ir.Function(self.module, pow_ty, name="pow")
            self.functions["pow"] = pow_func
        was_int = isinstance(left.type, ir.IntType) and isinstance(
            right.type, ir.IntType
        )
        if isinstance(left.type, ir.IntType):
            left = self.builder.sitofp(left, ir.DoubleType())
        if isinstance(right.type, ir.IntType):
            right = self.builder.sitofp(right, ir.DoubleType())
        result = self.builder.call(pow_func, [left, right])
        if was_int:
            return self.builder.fptosi(result, ir.IntType(32))
        return result

    def _normalize_ptr_cmp(self, left, right):
        if isinstance(left.type, ir.PointerType) and isinstance(right.type, ir.IntType):
            return left, ir.Constant(left.type, None)
        if isinstance(right.type, ir.PointerType) and isinstance(left.type, ir.IntType):
            return ir.Constant(right.type, None), right
        if isinstance(left.type, ir.PointerType) and isinstance(
            right.type, ir.PointerType
        ):
            return self.builder.ptrtoint(left, _i64), self.builder.ptrtoint(right, _i64)
        if isinstance(left.type, ir.PointerType) and isinstance(
            right.type, (ir.FloatType, ir.DoubleType)
        ):
            return self.builder.ptrtoint(left, _i64), self.builder.fptosi(right, _i64)
        if isinstance(right.type, ir.PointerType) and isinstance(
            left.type, (ir.FloatType, ir.DoubleType)
        ):
            return self.builder.fptosi(left, _i64), self.builder.ptrtoint(right, _i64)
        return left, right

    def _is_string_concat(self, node):
        left_is_str = getattr(node.left, "inferred_type", None) == "str"
        right_is_str = getattr(node.right, "inferred_type", None) == "str"
        if left_is_str or right_is_str:
            return True
        if (
            isinstance(node.left, Variable)
            and self.local_types.get(node.left.name) == "str"
        ):
            return True
        return bool(
            isinstance(node.right, Variable)
            and self.local_types.get(node.right.name) == "str"
        )

    def _emit_dyn_binop(self, node):
        dynop_map = {
            TokenType.PLUS: _DYNOP_ADD,
            TokenType.MINUS: _DYNOP_SUB,
            TokenType.STAR: _DYNOP_MUL,
            TokenType.SLASH: _DYNOP_DIV,
            TokenType.SLASH_SLASH: _DYNOP_FLOOR_DIV,
            TokenType.PERCENT: _DYNOP_MOD,
            TokenType.EQ_EQ: _DYNOP_EQ,
            TokenType.NOT_EQ: _DYNOP_NE,
            TokenType.LESS: _DYNOP_LT,
            TokenType.LESS_EQ: _DYNOP_LE,
            TokenType.GREATER: _DYNOP_GT,
            TokenType.GREATER_EQ: _DYNOP_GE,
            TokenType.AMPERSAND: _DYNOP_BAND,
            TokenType.PIPE: _DYNOP_BOR,
            TokenType.CARET: _DYNOP_BXOR,
            TokenType.SHL: _DYNOP_SHL,
            TokenType.SHR: _DYNOP_SHR,
        }
        op = dynop_map[node.op]
        k1, b1 = self._dyn_pair(node.left)
        k2, b2 = self._dyn_pair(node.right)
        fn = self.functions.get("dyn_op")
        out_ptr = self._alloca(_DynValue, name="dyn.binop")
        self.builder.call(fn, [out_ptr, ir.Constant(_i32, op), k1, b1, k2, b2])
        return self.builder.load(out_ptr)

    def _emit_string_concat(self, node):
        left = self.emit(node.left)
        right = self.emit(node.right)
        return self._concat_strings(left, right)

    def _get_strlen_fn(self):
        fn = self._strlen_fn
        if fn is not None:
            return fn
        for f in self.module.functions:
            if f.name == "strlen":
                self._strlen_fn = f
                return f
        # strlen returns size_t (i64 on every cpyte target), not int.
        fnty = ir.FunctionType(_i64, [_i8ptr])
        fn = ir.Function(self.module, fnty, "strlen")
        self._strlen_fn = fn
        return fn

    def _get_memcpy_fn(self):
        fn = self._memcpy_fn
        if fn is not None:
            return fn
        for f in self.module.functions:
            if f.name == "memcpy":
                self._memcpy_fn = f
                return f
        # n is size_t (i64 on every cpyte target).
        fnty = ir.FunctionType(_i8ptr, [_i8ptr, _i8ptr, _i64])
        fn = ir.Function(self.module, fnty, "memcpy")
        self._memcpy_fn = fn
        return fn

    @register_emitter(UnaryOp)
    def emit_unaryop(self, node: UnaryOp):
        if not self.no_userspace and self._is_dynamic_expr(node.operand):
            if node.op == TokenType.NOT:
                k, b = self._dyn_pair(node.operand)
                fn = self.functions.get("dyn_truthy_v")
                c = self.builder.call(fn, [k, b])
                return self.builder.icmp_unsigned("==", c, ir.Constant(_i32, 0))
            if node.op == TokenType.MINUS:
                k, b = self._dyn_pair(node.operand)
                fn = self.functions.get("dyn_op1")
                out_ptr = self._alloca(_DynValue, name="dyn.unop")
                self.builder.call(fn, [out_ptr, ir.Constant(_i32, _DYNOP_NEG), k, b])
                return self.builder.load(out_ptr)
        match node.op:
            case TokenType.PLUS:
                return self.emit(node.operand)
            case TokenType.MINUS:
                value = self.emit(node.operand)
                if self._is_big(node.operand) and isinstance(
                    value.type, ir.PointerType
                ):
                    return self.builder.call(self.functions["bigint_neg"], [value])
                if isinstance(value.type, ir.PointerType):
                    int_ty = ir.IntType(64)
                    as_int = self.builder.ptrtoint(value, int_ty)
                    neg = self.builder.sub(ir.Constant(int_ty, 0), as_int)
                    return self.builder.inttoptr(neg, value.type)
                zero = ir.Constant(
                    value.type, 0.0 if isinstance(value.type, ir.DoubleType) else 0
                )
                if isinstance(value.type, ir.DoubleType):
                    return self.builder.fsub(zero, value)
                return self.builder.sub(zero, value)
            case TokenType.MINUS_MINUS:
                value = self.emit(node.operand)
                if isinstance(value.type, ir.PointerType):
                    neg_one = ir.Constant(_i32, -1)
                    return self.builder.gep(value, [neg_one], inbounds=True)
                one = ir.Constant(
                    value.type, 1.0 if isinstance(value.type, ir.DoubleType) else 1
                )
                if isinstance(value.type, ir.DoubleType):
                    return self.builder.fsub(value, one)
                return self.builder.sub(value, one)
            case TokenType.TILDE:
                value = self.emit(node.operand)
                if isinstance(value.type, ir.PointerType):
                    int_ty = ir.IntType(64)
                    as_int = self.builder.ptrtoint(value, int_ty)
                    all_ones = ir.Constant(int_ty, -1)
                    xored = self.builder.xor(as_int, all_ones)
                    return self.builder.inttoptr(xored, value.type)
                if isinstance(value.type, (ir.FloatType, ir.DoubleType)):
                    int_ty = (
                        ir.IntType(32)
                        if isinstance(value.type, ir.FloatType)
                        else ir.IntType(64)
                    )
                    as_int = self.builder.bitcast(value, int_ty)
                    all_ones = ir.Constant(int_ty, -1)
                    xored = self.builder.xor(as_int, all_ones)
                    return self.builder.bitcast(xored, value.type)
                all_ones = ir.Constant(value.type, -1)
                return self.builder.xor(value, all_ones)
            case TokenType.NOT:
                value = self.emit(node.operand)
                if isinstance(value.type, ir.PointerType):
                    zero = ir.Constant(value.type, None)
                    return self.builder.icmp_unsigned("==", value, zero)
                if isinstance(value.type, (ir.FloatType, ir.DoubleType)):
                    zero = ir.Constant(value.type, 0.0)
                    return self.builder.fcmp_unordered("==", value, zero)
                zero = ir.Constant(value.type, 0)
                return self.builder.icmp_unsigned("==", value, zero)
        return None

    # Added constant check code
    # Is it good man?
    @register_emitter(Variable)
    def emit_variable(self, node):
        if self._is_decorator_factory:
            if node.name == "result":
                return self.builder.load(self._code_result, "result")
            if node.name == "func_name":
                return self.builder.load(self._code_fn_name, "code_fn_name")
            if node.name == "args":
                return self.builder.load(self._code_args, "code_args")

        const = self.const_vars.get(node.name)
        if const is not None:
            # Compile-time constant
            if isinstance(const, ir.Constant):
                return const
            # Runtime constant stored in immutable global
            return self.builder.load(const, node.name)

        # Constants resolved during semantic analysis (enum members, imported C constants)
        if getattr(node, "const_value", None) is not None:
            val = node.const_value
            # Use i32 for small values, i64 for large ones
            if -(2**31) <= val < 2**31:
                return ir.Constant(ir.IntType(32), val)
            return ir.Constant(ir.IntType(64), val)

        # Dynamic variable (promoted because its type changed at runtime)
        if getattr(node, "dynamic", False) and not self.no_userspace:
            ptr = self.locals.get(node.name)
            if ptr is not None and getattr(ptr.type, "pointee", None) == _DynValue:
                return self.builder.load(ptr, node.name)
            return self._emit_dyn_load(node)

        ptr = self.locals.get(node.name)
        if ptr is not None:
            return self.builder.load(ptr, node.name)

        gv = self.global_vars.get(node.name)
        if gv is not None:
            return self.builder.load(gv, node.name)

        ssa = self.ssa_values.get(node.name)
        if ssa is not None:
            return ssa

        func = self.functions.get(node.name)
        if func is not None:
            return self.builder.bitcast(func, ir.PointerType(ir.IntType(8)))

        raise Exception(
            f"Undefined variable '{node.name}' at L{node._token.line}:{node._token.column}"
        )

    def _trunc_or_ext(self, value, target_type):
        ty = target_type
        if isinstance(ty, ir.IntType) and isinstance(value.type, ir.IntType):
            if value.type.width < ty.width:
                if value.type.width == 32:
                    return self.builder.sext(value, ty)
                return self.builder.zext(value, ty)
            if value.type.width > ty.width:
                return self.builder.trunc(value, ty)
        return value

    def _char_to_str(self, value):
        """Convert an i8 char value into a 1-char heap-allocated string (str).

        cpy's analyzer allows char -> str in initializers, assignments and
        concatenation. Without this, a char is bit-cast to a pointer
        (`inttoptr`) and the resulting str dereferences a bogus address.
        """
        malloc_fn = self._get_malloc_fn()
        new_str = self.builder.call(malloc_fn, [ir.Constant(_i64, 2)])
        addr = self.builder.gep(new_str, [ir.Constant(_i32, 0)], inbounds=True)
        self.builder.store(value, addr)
        null_byte = self.builder.gep(new_str, [ir.Constant(_i32, 1)], inbounds=True)
        self.builder.store(ir.Constant(_i8, 0), null_byte)
        return new_str

    def _is_i8_to_str(self, value, pointee):
        """True when storing an i8 char into a str (i8*) slot."""
        return (
            isinstance(value.type, ir.IntType)
            and value.type.width == 8
            and isinstance(pointee, ir.PointerType)
            and getattr(pointee.pointee, "width", None) == 8
        )

    def _coerce_store(self, value, pointee):
        if isinstance(pointee, ir.IntType) and isinstance(value.type, ir.IntType):
            return self._trunc_or_ext(value, pointee)
        if isinstance(pointee, ir.PointerType) and isinstance(value.type, ir.IntType):
            if self._is_i8_to_str(value, pointee):
                return self._char_to_str(value)
            i64_ty = ir.IntType(64)
            if value.type.width < 64:
                if value.type.width == 32:
                    value = self.builder.sext(value, i64_ty)
                else:
                    value = self.builder.zext(value, i64_ty)
            return self.builder.inttoptr(value, pointee)
        if isinstance(pointee, ir.PointerType) and isinstance(
            value.type, ir.PointerType
        ):
            return self.builder.bitcast(value, pointee)
        if isinstance(pointee, ir.IntType) and isinstance(value.type, ir.PointerType):
            if (
                pointee.width == 8
                and isinstance(value.type.pointee, ir.IntType)
                and value.type.pointee.width == 8
            ):  # type: ignore[attr-defined]
                loaded = self.builder.load(value)
                if loaded.type != pointee:
                    loaded = self._trunc_or_ext(loaded, pointee)
                return loaded
            i64_ty = ir.IntType(64)
            ptr_val = self.builder.ptrtoint(value, i64_ty)
            if pointee.width < 64:
                return self.builder.trunc(ptr_val, pointee)
            return ptr_val
        if isinstance(pointee, (ir.FloatType, ir.DoubleType)) and isinstance(
            value.type, ir.IntType
        ):
            return self.builder.sitofp(value, pointee)
        if isinstance(pointee, (ir.FloatType, ir.DoubleType)) and isinstance(
            value.type, (ir.FloatType, ir.DoubleType)
        ):
            src_w = 64 if isinstance(value.type, ir.DoubleType) else 32
            dst_w = 64 if isinstance(pointee, ir.DoubleType) else 32
            return (
                self.builder.fpext(value, pointee)
                if src_w < dst_w
                else self.builder.fptrunc(value, pointee)
            )
        if isinstance(pointee, ir.IntType) and isinstance(
            value.type, (ir.FloatType, ir.DoubleType)
        ):
            return self.builder.fptosi(value, pointee)
        if isinstance(pointee, (ir.FloatType, ir.DoubleType)) and isinstance(
            value.type, ir.PointerType
        ):
            i64_ty = ir.IntType(64)
            ptr_val = self.builder.ptrtoint(value, i64_ty)
            return self.builder.sitofp(ptr_val, pointee)
        if isinstance(pointee, ir.PointerType) and isinstance(
            value.type, (ir.FloatType, ir.DoubleType)
        ):
            i64_ty = ir.IntType(64)
            int_val = self.builder.fptosi(value, i64_ty)
            return self.builder.inttoptr(int_val, pointee)
        return value

    def _pointee_type(self, ptr):
        try:
            return ptr.type.pointee
        except Exception:
            return None

    @register_emitter(Assign)
    def emit_assign(self, node):
        if (
            self._is_decorator_factory
            and isinstance(node.target, Variable)
            and node.target.name == "result"
        ):
            value = self.emit(node.value)
            if value.type != _DynValue:
                kind, bits = self._box_dyn(
                    value, getattr(node.value, "inferred_type", None)
                )
                value = self.builder.insert_value(
                    ir.Constant(_DynValue, ir.Undefined), ir.Constant(_i32, kind), 0
                )
                value = self.builder.insert_value(value, bits, 1)
            self.builder.store(value, self._code_result)
            return None
        if (
            self._is_decorator_factory
            and isinstance(node.target, Variable)
            and node.target.name == "skip"
        ):
            sk = self.emit(node.value)
            if sk.type != ir.IntType(1):
                sk = self._is_true(sk)
            self.builder.store(sk, self._code_skip)
            return None
        if isinstance(node.target, Index) and not self.no_userspace:
            obj_t = _bc_array_norm(getattr(node.target.obj, "inferred_type", None))
            if (
                obj_t == "dynamic"
                or obj_t == "dynamic[]"
                or self._is_dynamic_expr(node.target.obj)
            ):
                return self._emit_dyn_index_store(node)
        if isinstance(node.target, Variable):
            name = node.target.name

            # Constant assignment check :)
            if name in self.const_vars:
                raise Exception(
                    f"Cannot assign to constant '{name}' at L{node._token.line}:{node._token.column}"
                )

            if getattr(node, "dynamic", False) and not self.no_userspace:
                value = self.emit(node.value)
                self._emit_dyn_store(
                    name, value, getattr(node.value, "inferred_type", None)
                )
                return None

            ptr = self.locals.get(name)
            if ptr is not None:
                if self._is_dynamic_expr(node.value):
                    value = self._unbox_dyn_to(
                        node.value, self.local_types.get(name) or "int"
                    )
                else:
                    value = self.emit(node.value)
                pointee = self._pointee_type(ptr)
                if pointee and value.type != pointee:
                    decl_ty = self.local_types.get(name)
                    if decl_ty in ("big", "ubig") and not self._is_biglike(node.value):
                        src_t = getattr(node.value, "inferred_type", None)
                        value = (
                            self._promote_to_ubig(value)
                            if decl_ty == "ubig"
                            else self._promote_to_big(value, src_t)
                        )
                    elif isinstance(value.type, ir.PointerType) and isinstance(
                        pointee, ir.PointerType
                    ):
                        # llvmlite can mint two distinct pointer type objects
                        # (opaque `ptr` vs `_TypedPointerType`) that both print
                        # `i8*` but compare unequal; bitcast re-unifies them.
                        value = self.builder.bitcast(value, pointee)
                        if value.type != pointee:
                            value = self._coerce_store(value, pointee)
                    else:
                        value = self._coerce_store(value, pointee)
                var_type = self.local_types.get(name)
                if var_type in ("int64", "uint64"):
                    value = self._extend_to_i64(value)
                self.builder.store(value, ptr)
                cv = _const_int_value(node.value)
                self._set_const_prop(name, cv)
                return None
            value = self.emit(node.value)
            ssa = self.ssa_values.pop(name, None)
            ptr = self._alloca(value.type, name)
            self._declare_local(name, ptr, str(value.type))
            self.builder.store(value, ptr)
            self._set_const_prop(name, _const_int_value(node.value))
            return None
        if isinstance(node.target, str):
            name = node.target

            # Constant assignment check :) (AGAIN!)
            if name in self.const_vars:
                raise Exception(
                    f"Cannot assign to constant '{name}' at L{node._token.line}:{node._token.column}"
                )

            if getattr(node, "dynamic", False) and not self.no_userspace:
                value = self.emit(node.value)
                self._emit_dyn_store(
                    name, value, getattr(node.value, "inferred_type", None)
                )
                return None

            ptr = self.locals.get(name)
            if ptr is not None:
                if self._is_dynamic_expr(node.value):
                    value = self._unbox_dyn_to(
                        node.value, self.local_types.get(name) or "int"
                    )
                else:
                    value = self.emit(node.value)
                pointee = self._pointee_type(ptr)
                if pointee and value.type != pointee:
                    decl_ty = self.local_types.get(name)
                    if decl_ty in ("big", "ubig") and not self._is_biglike(node.value):
                        src_t = getattr(node.value, "inferred_type", None)
                        value = (
                            self._promote_to_ubig(value)
                            if decl_ty == "ubig"
                            else self._promote_to_big(value, src_t)
                        )
                    elif isinstance(value.type, ir.PointerType) and isinstance(
                        pointee, ir.PointerType
                    ):
                        # llvmlite can mint two distinct pointer type objects
                        # (opaque `ptr` vs `_TypedPointerType`) that both print
                        # `i8*` but compare unequal; bitcast re-unifies them.
                        value = self.builder.bitcast(value, pointee)
                        if value.type != pointee:
                            value = self._coerce_store(value, pointee)
                    else:
                        value = self._coerce_store(value, pointee)
                var_type = self.local_types.get(name)
                if var_type in ("int64", "uint64"):
                    value = self._extend_to_i64(value)
                self.builder.store(value, ptr)
                self._set_const_prop(name, _const_int_value(node.value))
                return None
            value = self.emit(node.value)
            ssa = self.ssa_values.pop(name, None)
            ptr = self._alloca(value.type, name)
            self._declare_local(name, ptr, str(value.type))
            self.builder.store(value, ptr)
            return None

        target_ptr = self._emit_lvalue(node.target)
        value = self.emit(node.value)
        pointee = self._pointee_type(target_ptr)
        if pointee and value.type != pointee:
            value = self._coerce_store(value, pointee)
        self.builder.store(value, target_ptr)
        # Write barrier for pointer stores into heap objects (tri-color invariant)
        if isinstance(pointee, ir.PointerType):
            self._emit_write_barrier(target_ptr, value)
        return None

    @register_emitter(Call)
    def emit_call(self, node):
        # Handle method calls: obj.method(args) -> ClassName.method(obj, args)
        if isinstance(node.callee, Attr):
            callee_name = node.callee.name
            obj_val = self.emit(node.callee.obj)
            # Look up the method by finding the class type of the object
            obj_type_name = None
            if isinstance(node.callee.obj, Variable):
                obj_type_name = self.local_types.get(node.callee.obj.name)
            elif hasattr(node.callee.obj, "_inferred_type"):
                obj_type_name = node.callee.obj._inferred_type
            if obj_type_name:
                # Strip pointer
                obj_type_name = obj_type_name.removesuffix("*")
                # Resolve generic types
                resolved = self._resolve_generic_type(obj_type_name)
                if resolved is not None:
                    obj_type_name = resolved
                method_name = f"{obj_type_name}.{callee_name}"
                func = self.functions.get(method_name)
                if func:
                    args = [obj_val]
                    for arg in node.args:
                        args.append(self.emit(arg))
                    return self.builder.call(func, args)

        # Builtin conversion functions (Python-style): str()/int()/float()/double()
        # and free() (available under #nogc)
        if (
            isinstance(node.callee, Variable)
            and node.callee.name in ("str", "int", "float", "double", "free")
            and ("free" not in self.functions or node.callee.name != "free")
        ):
            name = node.callee.name
            arg = node.args[0]
            if name == "free":
                return self._emit_builtin_free(arg)
            if name == "str":
                return self._emit_builtin_str(arg)
            if name == "int":
                return self._emit_builtin_int(arg)
            return self._emit_builtin_float(arg)

        # Builtin range() — Python-style range returning a registered int64 array.
        # Falls through to a user-defined `def range(...)` when present.
        if (
            isinstance(node.callee, Variable)
            and node.callee.name == "range"
            and "range" not in self.functions
        ):
            return self._emit_builtin_range(node)

        # Builtin str_split(str, sep=" ") — split a string into a DynValue list.
        if isinstance(node.callee, Variable) and node.callee.name == "str_split":
            return self._emit_builtin_str_split(node)

        # Builtin append(arr, x) — grow any registered array by one element.
        # Shadowable by a user `def append(...)`; disabled on bare-metal targets.
        if (
            isinstance(node.callee, Variable)
            and node.callee.name == "append"
            and "append" not in self.functions
        ):
            return self._emit_builtin_append(node)

        # Builtin len(x) — registered element count of an array. Shadowable by
        # a user `def len(...)`.
        if (
            isinstance(node.callee, Variable)
            and node.callee.name == "len"
            and "len" not in self.functions
        ):
            return self._emit_builtin_len(node)

        # Builtin code() — call the original function through __code_fn pointer.
        if (
            self._is_decorator_factory
            and isinstance(node.callee, Variable)
            and node.callee.name == "code"
        ):
            return self._emit_builtin_code(node)

        # Handle known macro functions by inlining
        if node.callee.name == "CGEventMaskBit":
            arg = self.emit(node.args[0])
            if isinstance(arg.type, ir.IntType):
                one = ir.Constant(arg.type, 1)
                shifted = self.builder.shl(one, arg)
                # Extend to i64 for CGEventMask (uint64_t)
                if shifted.type.width < 64:
                    shifted = self.builder.zext(shifted, ir.IntType(64))
                return shifted
            return ir.Constant(ir.IntType(64), 0)

        func = self.functions.get(node.callee.name)
        if func is None:
            raise Exception(
                f"Undefined function '{node.callee.name}' at L{node._token.line}:{node._token.column}"
            )
        args = []
        param_types = getattr(node, "param_types", None)
        for i, arg in enumerate(node.args):
            val = self.emit(arg)
            if i < len(func.function_type.args):
                expected = func.function_type.args[i]
                if (
                    param_types
                    and i < len(param_types)
                    and param_types[i] in ("big", "ubig")
                    and isinstance(val.type, ir.IntType)
                ):
                    if param_types[i] == "ubig":
                        val = self._promote_to_ubig(val)
                    else:
                        val = self._promote_to_big(
                            val, getattr(arg, "inferred_type", None)
                        )
                if expected == _DynValue and val.type != _DynValue:
                    kind, bits = self._box_dyn(val, getattr(arg, "inferred_type", None))
                    val = self.builder.insert_value(
                        ir.Constant(_DynValue, ir.Undefined), ir.Constant(_i32, kind), 0
                    )
                    val = self.builder.insert_value(val, bits, 1)
                elif val.type == _DynValue and expected != _DynValue:
                    kind_v = self.builder.extract_value(val, 0)
                    data_v = self.builder.extract_value(val, 1)
                    if isinstance(expected, (ir.FloatType, ir.DoubleType)):
                        want = _DYN_DOUBLE
                    elif expected == _i8:
                        want = _DYN_CHAR
                    elif isinstance(expected, ir.IntType):
                        want = _DYN_INT
                    else:
                        want = _DYN_PTR
                    val = self.builder.call(
                        self.functions["dyn_as_v"],
                        [kind_v, data_v, ir.Constant(_i32, want)],
                    )
                if (
                    isinstance(val, ir.Constant)
                    and val.constant == 0
                    and isinstance(val.type, ir.IntType)
                ):
                    if isinstance(expected, ir.PointerType):
                        val = ir.Constant(expected, None)
                if (
                    isinstance(val.type, ir.IntType)
                    and isinstance(expected, ir.IntType)
                    and val.type.width != expected.width
                ):
                    if val.type.width < expected.width:
                        val = self.builder.sext(val, expected)
                    else:
                        val = self.builder.trunc(val, expected)
                if isinstance(val.type, ir.IntType) and isinstance(
                    expected, ir.PointerType
                ):
                    val = self.builder.inttoptr(val, expected)
                if isinstance(val.type, ir.PointerType) and isinstance(
                    expected, ir.IntType
                ):
                    val = self.builder.ptrtoint(val, expected)
                if (
                    isinstance(val.type, ir.PointerType)
                    and isinstance(expected, ir.PointerType)
                    and val.type != expected
                ):
                    val = self.builder.bitcast(val, expected)
                if isinstance(val.type, ir.IntType) and isinstance(
                    expected, ir.DoubleType
                ):
                    val = self.builder.sitofp(val, expected)
                if isinstance(val.type, ir.DoubleType) and isinstance(
                    expected, ir.IntType
                ):
                    val = self.builder.fptosi(val, expected)
            args.append(val)
        return self.builder.call(func, args)

    def _get_or_create_fn(self, name, ret, args):
        """Return an existing declaration with `name` or declare a fresh extern."""
        for f in self.module.functions:
            if f.name == name:
                return f
        return ir.Function(self.module, ir.FunctionType(ret, args), name)

    def _emit_builtin_str(self, arg):
        if not self.no_userspace and self._is_dynamic_expr(arg):
            k, b = self._dyn_pair(arg)
            return self.builder.call(self.functions["dyn_str_v"], [k, b])
        val = self.emit(arg)
        return self._stringify_value(arg, val)

    def _emit_builtin_int(self, arg):
        if not self.no_userspace and self._is_dynamic_expr(arg):
            k, b = self._dyn_pair(arg)
            bits = self.builder.call(
                self.functions["dyn_as_v"], [k, b, ir.Constant(_i32, _DYN_INT)]
            )
            return self.builder.trunc(bits, _i32)
        val = self.emit(arg)
        if getattr(arg, "inferred_type", None) in ("big", "ubig"):
            s = self.builder.call(self.functions["bigint_to_str"], [val])
            atoi = self._get_or_create_fn("atoi", _i32, [_i8ptr])
            return self.builder.call(atoi, [s])
        if isinstance(val.type, ir.PointerType):
            atoi = self._get_or_create_fn("atoi", _i32, [_i8ptr])
            if val.type != _i8ptr:
                val = self.builder.bitcast(val, _i8ptr)
            return self.builder.call(atoi, [val])
        if isinstance(val.type, ir.DoubleType):
            return self.builder.fptosi(val, _i32)
        if isinstance(val.type, ir.IntType):
            if val.type.width > 32:
                return self.builder.trunc(val, _i32)
            if val.type.width < 32:
                return self.builder.sext(val, _i32)
            return val
        raise Exception(f"cannot convert {val.type} to int")

    def _emit_builtin_float(self, arg):
        if not self.no_userspace and self._is_dynamic_expr(arg):
            k, b = self._dyn_pair(arg)
            bits = self.builder.call(
                self.functions["dyn_as_v"], [k, b, ir.Constant(_i32, _DYN_DOUBLE)]
            )
            return self.builder.bitcast(bits, ir.DoubleType())
        val = self.emit(arg)
        if getattr(arg, "inferred_type", None) in ("big", "ubig"):
            s = self.builder.call(self.functions["bigint_to_str"], [val])
            atof = self._get_or_create_fn("atof", ir.DoubleType(), [_i8ptr])
            return self.builder.call(atof, [s])
        if isinstance(val.type, ir.PointerType):
            atof = self._get_or_create_fn("atof", ir.DoubleType(), [_i8ptr])
            if val.type != _i8ptr:
                val = self.builder.bitcast(val, _i8ptr)
            return self.builder.call(atof, [val])
        if isinstance(val.type, ir.DoubleType):
            return val
        if isinstance(val.type, ir.IntType):
            if val.type.width < 64:
                val = self.builder.sext(val, _i64)
            return self.builder.sitofp(val, ir.DoubleType())
        raise Exception(f"cannot convert {val.type} to float")

    def _emit_builtin_range(self, node):
        """range(stop) / range(start, stop) / range(start, stop, step).

        Returns a gc_malloc'd int64 array registered in the array-length side
        table, so `for x in range(...)` iterates it and indexing/len/free all
        behave exactly like a `new int64[n]` array. Mirrors Python's range
        semantics: step != 0, empty for out-of-bounds, negative steps allowed.
        """
        args = node.args
        if not 1 <= len(args) <= 3:
            raise Exception(
                f"range() expects 1 to 3 arguments, got {len(args)} "
                f"at L{node._token.line}:{node._token.column}"
            )

        def _as_i64(val):
            if isinstance(val.type, ir.PointerType):
                return self.builder.ptrtoint(val, _i64)
            if isinstance(val.type, (ir.FloatType, ir.DoubleType)):
                return self.builder.fptosi(val, _i64)
            if isinstance(val.type, ir.IntType):
                return self._extend_to_i64(val)
            return val

        if len(args) == 1:
            start = ir.Constant(_i64, 0)
            stop = _as_i64(self.emit(args[0]))
            step = ir.Constant(_i64, 1)
        else:
            start = _as_i64(self.emit(args[0]))
            stop = _as_i64(self.emit(args[1]))
            step = (
                ir.Constant(_i64, 1) if len(args) == 2 else _as_i64(self.emit(args[2]))
            )

        # Python semantics: values start, start+step, ... while val < stop
        # (positive step) or val > stop (negative step). step == 0 is an error.
        zero = ir.Constant(_i64, 0)
        one = ir.Constant(_i64, 1)

        # Trap on step == 0 BEFORE any arithmetic (Python raises ValueError).
        trap_fn = self._get_trap_fn()
        with self.builder.if_then(self.builder.icmp_signed("==", step, zero)):
            self.builder.call(trap_fn, [])

        step_pos = self.builder.icmp_signed(">", step, zero)
        # For a negative step the sequence must be strictly descending.
        num = self.builder.select(
            step_pos, self.builder.sub(stop, start), self.builder.sub(start, stop)
        )
        dist = self.builder.select(step_pos, step, self.builder.sub(zero, step))
        count = self.builder.sdiv(
            self.builder.add(self.builder.sub(num, one), dist), dist
        )
        count = self.builder.select(
            self.builder.icmp_signed(">", count, zero), count, zero
        )

        malloc_fn = self._get_malloc_fn()
        eight = ir.Constant(_i64, 8)
        alloc_fn = self.functions.get("cpyte_array_alloc")
        if alloc_fn is not None and not self.no_userspace:
            buf = self.builder.call(alloc_fn, [eight, count])
        else:
            total = self.builder.mul(count, eight)
            buf = self.builder.call(malloc_fn, [total])
        buf8 = buf
        if buf8.type != _i8ptr:
            buf8 = self.builder.bitcast(buf8, _i8ptr)
        i64arr = self.builder.bitcast(buf8, ir.PointerType(_i64))

        i_ptr = self._alloca(_i64, name="range.i")
        self.builder.store(zero, i_ptr)
        cond_bb = self.builder.append_basic_block("range.cond")
        body_bb = self.builder.append_basic_block("range.body")
        end_bb = self.builder.append_basic_block("range.end")
        self.builder.branch(cond_bb)
        self.builder.position_at_end(cond_bb)
        i = self.builder.load(i_ptr)
        self.builder.cbranch(self.builder.icmp_signed("<", i, count), body_bb, end_bb)
        self.builder.position_at_end(body_bb)
        i_body = self.builder.load(i_ptr)
        val = self.builder.add(start, self.builder.mul(i_body, step))
        self.builder.store(val, self.builder.gep(i64arr, [i_body], inbounds=True))
        self.builder.store(self.builder.add(i_body, one), i_ptr)
        self.builder.branch(cond_bb)
        self.builder.position_at_end(end_bb)
        return i64arr

    def _emit_builtin_free(self, arg):
        val = self.emit(arg)
        if isinstance(val.type, ir.IntType):
            val = self.builder.inttoptr(val, _i8ptr)
        elif val.type != _i8ptr:
            val = self.builder.bitcast(val, _i8ptr)
        # Under GC, cpy `malloc` lowers to gc_malloc, so freeing must unlink
        # the object from the collector before releasing it; otherwise the
        # address stays in the GC sets and can be double-released after a
        # later allocation reuses it. cpyte_gc_free handles both cases.
        gc_free = self.functions.get("cpyte_gc_free")
        if gc_free is not None:
            return self.builder.call(gc_free, [val])
        free_fn = self._get_or_create_fn("free", _void, [_i8ptr])
        return self.builder.call(free_fn, [val])

    def _emit_builtin_str_split(self, node):
        """str_split(str) or str_split(str, sep) — split a string into a dynamic list."""
        args = node.args
        if not 1 <= len(args) <= 2:
            raise Exception(
                f"str_split() expects 1 or 2 arguments, got {len(args)} "
                f"at L{node._token.line}:{node._token.column}"
            )
        str_val = self.emit(args[0])
        if str_val.type != _i8ptr:
            str_val = self.builder.bitcast(str_val, _i8ptr)
        if len(args) == 2:
            sep_val = self.emit(args[1])
            if sep_val.type != _i8ptr:
                sep_val = self.builder.bitcast(sep_val, _i8ptr)
        else:
            sep_val = self._string_const(" ")
        fn = self.functions.get("str_split")
        if fn is None:
            fn = ir.Function(
                self.module,
                ir.FunctionType(_DynValuePtr, [_i8ptr, _i8ptr]),
                name="str_split",
            )
            self.functions["str_split"] = fn
        return self.builder.call(fn, [str_val, sep_val])

    def _emit_builtin_len(self, node):
        """len(arr) — return the registered element count of an array.

        Uses the array-length side table (cpyte_array_len), so it works for
        `new T[n]`, `range()`, list literals and `str_split()` results. Not
        supported (semantically rejected) on fixed-size local arrays or
        raw/C buffers that were never length-registered."""
        arg = self.emit(node.args[0])
        if arg.type != _i8ptr:
            arg = self.builder.bitcast(arg, _i8ptr)
        fn = self.functions.get("cpyte_array_len")
        if fn is None:
            fn = ir.Function(
                self.module,
                ir.FunctionType(_i64, [_i8ptr]),
                name="cpyte_array_len",
            )
            self.functions["cpyte_array_len"] = fn
        return self.builder.call(fn, [arg])

    def _emit_builtin_append(self, node):
        """append(arr, x) — grow a registered array by one element.

        `arr` must be an lvalue (a variable, an array/`dynamic[]` element that
        holds a list, or a struct field) whose type is `T[]` / `dynamic[]`. The
        buffer is grown with cpyte_array_reserve (plain malloc) or
        gc_array_reserve (GC build — the collector owns payloads so libc realloc
        would orphan the header). The new element is stored and the (possibly
        moved) array pointer is written back into the lvalue and returned, so
        both `append(a, x)` and `a = append(a, x)` work.
        """
        if self.no_userspace:
            raise Exception(
                f"append() is not available on userspace-disabled "
                f"targets ({node._token.line}:{node._token.column})"
            )
        if len(node.args) != 2:
            raise Exception(
                f"append() expects 2 arguments, got {len(node.args)} "
                f"at L{node._token.line}:{node._token.column}"
            )
        target = node.args[0]
        value = node.args[1]
        arr_t = getattr(target, "inferred_type", None) or ""
        if not arr_t.endswith("[]") and arr_t != "dynamic":
            raise Exception(
                f"append() target must be an array lvalue, got '{arr_t}' "
                f"at L{target._token.line}:{target._token.column}"
            )

        slot = self._emit_lvalue(target)
        cur = self.builder.load(slot, "append.cur")

        elem_t = arr_t[:-2] if arr_t.endswith("[]") else arr_t
        if arr_t == "dynamic":
            # Element slot holds a DynValue; its i64 data is the inner list ptr.
            elem_llvm = _DynValue
            inner_data = self.builder.extract_value(cur, 1)
            inner_ptr = self.builder.inttoptr(inner_data, _i8ptr)
            len_fn = self.functions.get("cpyte_array_len")
            idx = self.builder.call(len_fn, [inner_ptr])
            elem_size = self.builder.zext(self._sizeof_type(elem_llvm), _i64)
            new_ptr = self.builder.call(
                self._get_reserve_fn(),
                [inner_ptr, elem_size, ir.Constant(_i64, 1)],
            )
            new_data = self.builder.ptrtoint(new_ptr, _i64)
            moved = self.builder.icmp_unsigned("!=", inner_data, new_data)
            with self.builder.if_then(moved):
                inner_dv = self.builder.insert_value(cur, new_data, 1)
                self.builder.store(inner_dv, slot)
            value_rv = self.emit(value)
            if value_rv.type == _DynValue:
                elem = value_rv
            else:
                kind, bits = self._box_dyn(
                    value_rv, getattr(value, "inferred_type", None)
                )
                elem = self.builder.insert_value(
                    ir.Constant(_DynValue, ir.Undefined),
                    ir.Constant(_i32, kind),
                    0,
                )
                elem = self.builder.insert_value(elem, bits, 1)
            elem_ptr = self.builder.gep(
                self.builder.bitcast(new_ptr, _DynValuePtr), [idx], inbounds=True
            )
            self.builder.store(elem, elem_ptr)
            if not self.no_gc and isinstance(value_rv.type, ir.PointerType):
                self._emit_write_barrier(
                    self.builder.bitcast(elem_ptr, _i8ptr),
                    self.builder.bitcast(value_rv, _i8ptr),
                )
            # Bump the header length for the inner list.
            new_len = self.builder.add(idx, ir.Constant(_i64, 1))
            setlen_fn = self.functions.get("cpyte_array_set_len")
            if setlen_fn is not None:
                self.builder.call(
                    setlen_fn,
                    [self.builder.bitcast(new_ptr, _i8ptr), new_len],
                )
            if arr_t == "dynamic":
                # Appending into a dynamic element (`dynarr[i]` / a dynamic
                # struct field): the append returns the inner list as a tagged
                # DynValue, not a raw pointer.
                dv = self.builder.insert_value(
                    ir.Constant(_DynValue, ir.Undefined),
                    ir.Constant(_i32, _DYN_LIST),
                    0,
                )
                dv = self.builder.insert_value(dv, new_data, 1)
                return dv
            return self.builder.bitcast(new_ptr, self.llvm_type(arr_t))

        # Typed array element. Standard T[] path: grow, store at index == old len,
        # write the possibly-moved pointer back into the lvalue slot.
        elem_llvm = self.llvm_type(elem_t)
        elem_size = self.builder.zext(self._sizeof_type(elem_llvm), _i64)
        len_fn = self.functions.get("cpyte_array_len")
        idx = self.builder.call(len_fn, [self.builder.bitcast(cur, _i8ptr)])
        new_ptr = self.builder.call(
            self._get_reserve_fn(),
            [self.builder.bitcast(cur, _i8ptr), elem_size, ir.Constant(_i64, 1)],
        )
        moved = self.builder.icmp_unsigned(
            "!=",
            self.builder.bitcast(cur, _i8ptr),
            self.builder.bitcast(new_ptr, _i8ptr),
        )
        with self.builder.if_then(moved):
            slot_elem_ty = slot.type.pointee
            self.builder.store(self.builder.bitcast(new_ptr, slot_elem_ty), slot)
        value_rv = self.emit(value)
        if elem_llvm == _DynValue and value_rv.type != _DynValue:
            kind, bits = self._box_dyn(value_rv, getattr(value, "inferred_type", None))
            value_rv = self.builder.insert_value(
                ir.Constant(_DynValue, ir.Undefined),
                ir.Constant(_i32, kind),
                0,
            )
            value_rv = self.builder.insert_value(value_rv, bits, 1)
        if value_rv.type == _DynValue and elem_llvm != _DynValue:
            kind_v = self.builder.extract_value(value_rv, 0)
            data_v = self.builder.extract_value(value_rv, 1)
            if isinstance(elem_llvm, (ir.FloatType, ir.DoubleType)):
                want = _DYN_DOUBLE
            elif elem_llvm == _i8:
                want = _DYN_CHAR
            elif isinstance(elem_llvm, ir.IntType):
                want = _DYN_INT
            else:
                want = _DYN_PTR
            value_rv = self.builder.call(
                self.functions["dyn_as_v"],
                [kind_v, data_v, ir.Constant(_i32, want)],
            )
        elem_ptr = self.builder.gep(
            self.builder.bitcast(new_ptr, ir.PointerType(elem_llvm)),
            [idx],
            inbounds=True,
        )
        self.builder.store(self._coerce_store(value_rv, elem_llvm), elem_ptr)
        if not self.no_gc and isinstance(value_rv.type, ir.PointerType):
            self._emit_write_barrier(
                self.builder.bitcast(elem_ptr, _i8ptr),
                self.builder.bitcast(value_rv, _i8ptr),
            )
        # Bump the header length so len() reflects the new element.
        new_len = self.builder.add(idx, ir.Constant(_i64, 1))
        setlen_fn = self.functions.get("cpyte_array_set_len")
        if setlen_fn is not None:
            self.builder.call(
                setlen_fn, [self.builder.bitcast(new_ptr, _i8ptr), new_len]
            )
        return self.builder.bitcast(new_ptr, self.llvm_type(arr_t))

    def _get_reserve_fn(self):
        """Growth helper for the active allocator (gc_array_reserve when the GC
        runtime is linked, cpyte_array_reserve otherwise)."""
        dry = self.functions.get("gc_array_reserve") if not self.no_gc else None
        if dry is not None:
            return dry
        if not self.no_gc:
            fnty = ir.FunctionType(_i8ptr, [_i8ptr, _i64, _i64])
            fn = ir.Function(self.module, fnty, "gc_array_reserve")
            self.functions["gc_array_reserve"] = fn
            return fn
        cpy = self.functions.get("cpyte_array_reserve")
        if cpy is not None:
            return cpy
        fnty = ir.FunctionType(_i8ptr, [_i8ptr, _i64, _i64])
        fn = ir.Function(self.module, fnty, "cpyte_array_reserve")
        self.functions["cpyte_array_reserve"] = fn
        return fn

    def _emit_builtin_code(self, node):
        """code() — call the original function via __code_fn pointer and store result in __code_result."""
        # Load the stored function pointer
        fn_ptr = self.builder.load(self._code_fn_ptr, "code_fn_ptr")
        # The pointer is a raw i8*; we cast to a DynValue()-function type and call
        code_fnty = ir.FunctionType(_DynValue, [])
        fn_typed = self.builder.bitcast(fn_ptr, ir.PointerType(code_fnty))
        result = self.builder.call(fn_typed, [])
        self.builder.store(result, self._code_result)
        return result

    @register_emitter(Print)
    def emit_print(self, node):
        values = node.value
        if not values:
            if self.no_userspace:
                return None
            return self.builder.call(
                self.functions["print_str"], [self._string_const("")]
            )
        if self.no_userspace:
            # In no-userspace mode, print statements become no-ops
            # Still emit the value for side effects, but don't call print function
            for expr in values:
                self.emit(expr)
            return None
        if len(values) == 1:
            return self._emit_print_value(values[0])
        # Multi-argument print: stringify each value, join with single spaces,
        # and print once (Python-style: print("a", 1) -> "a 1").
        parts = []
        for expr in values:
            val = self.emit(expr)
            parts.append(self._stringify_value(expr, val))
        acc = parts[0]
        space = self._string_const(" ")
        for part in parts[1:]:
            acc = self._concat_strings(acc, space)
            acc = self._concat_strings(acc, part)
        return self.builder.call(self.functions["print_str"], [acc])

    def _emit_print_value(self, expr):
        if not self.no_userspace and self._is_dynamic_expr(expr):
            k, b = self._dyn_pair(expr)
            return self.builder.call(self.functions["dyn_print_v"], [k, b])

        value = self.emit(expr)
        if value.type == _DynValue and not self.no_userspace:
            k = self.builder.extract_value(value, 0)
            b = self.builder.extract_value(value, 1)
            return self.builder.call(self.functions["dyn_print_v"], [k, b])
        if self._is_biglike(expr):
            return self.builder.call(self.functions["bigint_print"], [value])
        if isinstance(value.type, ir.DoubleType):
            return self.builder.call(self.functions["print_double"], [value])
        if (
            isinstance(value.type, ir.PointerType)
            and isinstance(value.type.pointee, ir.IntType)  # type: ignore[attr-defined]
            and value.type.pointee.width == 8
        ):  # type: ignore[attr-defined]
            return self.builder.call(self.functions["print_str"], [value])
        # Handle 64-bit integers
        if isinstance(value.type, ir.IntType) and value.type.width == 64:
            is_uint64 = False
            if isinstance(expr, Variable):
                var_type = self.local_types.get(expr.name)
                if var_type == "uint64":
                    is_uint64 = True
            elif hasattr(expr, "inferred_type") and expr.inferred_type == "uint64":
                is_uint64 = True
            elif isinstance(expr, BinOp):
                left_type = None
                right_type = None
                if isinstance(expr.left, Variable):
                    left_type = self.local_types.get(expr.left.name)
                elif hasattr(expr.left, "inferred_type"):
                    left_type = expr.left.inferred_type
                if isinstance(expr.right, Variable):
                    right_type = self.local_types.get(expr.right.name)
                elif hasattr(expr.right, "inferred_type"):
                    right_type = expr.right.inferred_type
                if left_type == "uint64" or right_type == "uint64":
                    is_uint64 = True
            if is_uint64:
                return self.builder.call(self.functions["print_uint64"], [value])
            return self.builder.call(self.functions["print_int64"], [value])
        # Handle bool (i1) by zero-extending to i32
        if isinstance(value.type, ir.IntType) and value.type.width == 1:
            value = self.builder.zext(value, _i32)
            return self.builder.call(self.functions["print_int"], [value])
        # Handle small int types (i8, i16, i32) by extending to i32 for print_int
        if isinstance(value.type, ir.IntType) and value.type.width < 32:
            value = self.builder.zext(value, _i32)
            return self.builder.call(self.functions["print_int"], [value])
        # Handle non-string pointers: convert to int64 and print as hex
        if isinstance(value.type, ir.PointerType):
            value = self.builder.ptrtoint(value, _i64)
            return self.builder.call(self.functions["print_hex"], [value])
        # Handle i32 — call print_int directly
        if isinstance(value.type, ir.IntType):
            return self.builder.call(self.functions["print_int"], [value])
        # Fallback: convert to int64 and print
        if isinstance(value.type, (ir.FloatType, ir.DoubleType)):
            return self.builder.call(self.functions["print_double"], [value])
        if isinstance(value.type, ir.PointerType):
            return self.builder.call(
                self.functions["print_hex"], [self.builder.ptrtoint(value, _i64)]
            )
        return self.builder.call(
            self.functions["print_int64"], [self.builder.ptrtoint(value, _i64)]
        )

    @register_emitter(Input)
    def emit_input(self, node):
        if self.no_userspace:
            # In no-userspace mode, input returns 0
            return ir.Constant(_i32, 0)
        func = self.functions["input"]
        return self.builder.call(func, [])

    @register_emitter(InputStr)
    def emit_inputstr(self, node):
        if self.no_userspace:
            # In no-userspace mode, input_str returns null pointer
            return ir.Constant(_i8ptr, None)
        func = self.functions["input_str"]
        return self.builder.call(func, [])

    @register_emitter(InputBig)
    def emit_inputbig(self, node):
        if self.no_userspace:
            # In no-userspace mode, input_big returns a zero bignum
            return self.builder.call(
                self.functions["bigint_from_int"], [ir.Constant(_i64, 0)]
            )
        func = self.functions["bigint_input"]
        return self.builder.call(func, [])

    @register_emitter(Signed67)
    def emit_signed67(self, node):
        key = b"cpyte-easter-egg-2024"
        sig = hmac.new(key, b"67", hashlib.sha256).hexdigest()
        return self._string_const(sig)

    @register_emitter(While)
    def emit_while(self, node):
        # ---- Static zero-trip loop: `while 0:` body never executes ----
        if isinstance(node.cond, Number):
            cv = _const_int_value(node.cond)
            if cv is not None and cv == 0:
                return

        # Const-prop across a loop backedge is unsound: clear the tracked
        # straight-line constants while we process the body, then restore.
        # A variable the body can write must be dropped from BOTH working
        # copies -- single-execution folds baked into the (re-executed) body
        # would otherwise compute wrong values, and a restored pre-loop entry
        # would leave a stale constant visible to reads after the loop.
        saved_const_prop = self._const_prop
        written_here = _loop_written_names(node)
        active = dict(saved_const_prop)
        for name in written_here:
            active.pop(name, None)
        self._const_prop = active
        saved_const_prop_f = self._const_prop_f
        active_f = dict(saved_const_prop_f)
        for name in written_here:
            active_f.pop(name, None)
        self._const_prop_f = active_f

        # ---- Loop-invariant code motion: hoist provably-invariant assigns ----
        invariants = _find_loop_invariants(node)
        hoisted_ids = set()
        for inv in invariants:
            if not self._block_terminated():
                self.emit(inv)
                hoisted_ids.add(id(inv))

        cond_bb = self.builder.append_basic_block("while.cond")
        body_bb = self.builder.append_basic_block("while.body")
        end_bb = self.builder.append_basic_block("while.end")

        self.builder.branch(cond_bb)
        self.builder.position_at_end(cond_bb)
        self._dbg_loop_head(node)

        cond = self._truthy_expr(node.cond)
        self.builder.cbranch(cond, body_bb, end_bb)
        self.builder.position_at_end(body_bb)

        self.loop_stack.append((cond_bb, end_bb))
        self._push_scope()
        for stmt in node.body:
            if id(stmt) in hoisted_ids:
                continue
            if not self._block_terminated():
                self.emit(stmt)
        self._pop_scope()
        self.loop_stack.pop()

        if not self._block_terminated():
            self.builder.branch(cond_bb)

        self.builder.position_at_end(end_bb)
        # Restore, dropping any entries the loop body could have changed.
        for name in written_here:
            saved_const_prop.pop(name, None)
            saved_const_prop_f.pop(name, None)
        self._const_prop = saved_const_prop
        self._const_prop_f = saved_const_prop_f

    # ------------------------------------------------------------------
    # 3.  Loop transformations: bounded unrolling
    # ------------------------------------------------------------------

    def _set_const_prop(self, name: str, const_val):
        """Record a compile-time integer value for variable *name* in the
        current straight-line region, so downstream reads can be replaced by
        a constant.  Passing *None* invalidates the entry (any reassignment
        or loop backedge)."""
        if const_val is not None:
            self._const_prop[name] = const_val
            self._const_prop_f.pop(name, None)
        else:
            self._const_prop.pop(name, None)
            self._const_prop_f.pop(name, None)

    def _is_const_var(self, name: str) -> bool:
        return name in self._const_prop

    def _const_var_value(self, name: str) -> Optional[int]:
        return self._const_prop.get(name)

    def _try_unroll_counted_loop(self, node: While, pending_ivs: dict) -> bool:
        """Fully unroll a ``while`` loop whose iteration count is a small
        compile-time constant.  *pending_ivs* maps constant-init variable
        names (``VarDecl`` with a numeric ``Number`` init) to their start
        value; the loop's induction variable must be one of them and the loop
        must be a counted loop (``i < stop`` ...) with a single ``i = i + k``
        / ``i = i - k`` increment in the body.  Returns ``True`` when it
        unrolled the loop in place.

        Each unrolled copy stores the canonical counter value into the
        induction variable's slot first, and also records it in the
        straight-line constant-propagation table so reads of the counter
        inside an iteration fold to a constant (as with ordinary control flow,
        the propagation is invalidated once the loop ends)."""
        if not pending_ivs:
            return False
        info = _is_simple_counted_loop(node)
        if info is None:
            return False
        iv_name, stop, step, inclusive = info
        start_val = pending_ivs.get(iv_name)
        if start_val is None:
            return False
        n = _count_loop_iterations(start_val, stop, step, inclusive)
        if n < 0 or n > 16:
            return False
        # Guard: the body must not contain break/continue (we cannot
        # redistribute control-flow edges across the unrolled copies).
        for stmt in node.body:
            if isinstance(stmt, (Break, Continue, Return, Raise)):
                return False
            if isinstance(stmt, (If, While)):
                return False
        # Guard: pointer/field/array writes (`*p = ...`, `a[i] = ...`) or
        # address-taking in the body could alias the counter's slot; unrolling
        # stores canonical counter values that would clobber those writes.
        if _loop_has_alias_risk(node.body):
            return False

        iv_ptr = self.locals.get(iv_name)
        if iv_ptr is None:
            iv_ptr = self._alloca(_i32, name=iv_name)
            self._declare_local(iv_name, iv_ptr, "int")
        iv_ty = self._pointee_type(iv_ptr) or _i32
        width = iv_ty.width if isinstance(iv_ty, ir.IntType) else 32

        def iv_const(v: int):
            # Wrap to the declared integer width (cpyte ints wrap like C).
            mask = (1 << width) - 1
            w = v & mask
            if w >= (1 << (width - 1)):
                w -= 1 << width
            return ir.Constant(iv_ty, w), w

        # Counter's declared type must be a plain int to safely const-propagate
        # its reads into downstream arithmetic (Number literals lower to i32).
        can_const_prop = self.local_types.get(iv_name) in ("int", "int32")

        # Zero-iteration loop: body never runs; counter keeps its init value.
        if n == 0:
            self.builder.store(iv_const(start_val)[0], iv_ptr)
            return True

        self._push_scope()
        for i in range(n):
            cv, raw = iv_const(start_val + i * step)
            self.builder.store(cv, iv_ptr)
            if can_const_prop:
                self._set_const_prop(iv_name, raw)
            for stmt in node.body:
                if not self._block_terminated():
                    self.emit(stmt)
        self._pop_scope()
        # The value after the loop is the first value that fails the
        # comparison (start + n*step). It is not a compile-time constant any
        # more, so invalidate the entry rather than leaving a stale fold.
        self._set_const_prop(iv_name, None)
        if not self._block_terminated():
            self.builder.store(iv_const(start_val + n * step)[0], iv_ptr)
        return True

    @register_emitter(Break)
    def emit_break(self, node):
        if not self.loop_stack:
            return
        _, end_bb = self.loop_stack[-1]
        self.builder.branch(end_bb)

    @register_emitter(Continue)
    def emit_continue(self, node):
        if not self.loop_stack:
            return
        cond_bb, _ = self.loop_stack[-1]
        self.builder.branch(cond_bb)

    @register_emitter(Assert)
    def emit_assert(self, node):
        cond = self._truthy_expr(node.cond)
        trap_fn = self._get_trap_fn()
        ok_bb = self.builder.append_basic_block("assert.ok")
        fail_bb = self.builder.append_basic_block("assert.fail")
        self.builder.cbranch(cond, ok_bb, fail_bb)
        self.builder.position_at_end(fail_bb)
        if node.message is not None:
            msg_val = self.emit(node.message)
            if msg_val.type != _i8ptr:
                msg_val = self.builder.bitcast(msg_val, _i8ptr)
            self.builder.call(self.functions["print_str"], [msg_val])
        fflush_fn = self._get_or_create_fn("fflush", _i32, [_i8ptr])
        self.builder.call(fflush_fn, [ir.Constant(_i8ptr, None)])
        self.builder.call(trap_fn, [])
        self.builder.unreachable()
        self.builder.position_at_end(ok_bb)

    def emit_for(self, node):
        var_name = node["var"]
        iterable = node["iter"]
        body = node["body"]
        iter_type = _bc_array_norm(node.get("iter_type")) or "str"
        var_type = node.get("var_type") or "char"
        saved_const_prop, saved_const_prop_f, for_mutated = self._enter_for_const_prop(
            var_name, node
        )
        try:
            return self._emit_for_worker(
                node, var_name, iterable, body, iter_type, var_type
            )
        finally:
            self._exit_for_const_prop(saved_const_prop, saved_const_prop_f, for_mutated)

    def _enter_for_const_prop(self, var_name, node):
        """Detach constant-propagation for a ``for`` loop.

        `emit_while` invalidates the entries of every name the loop writes, but
        `for` loops historically left `_const_prop` untouched.  With a prior
        bound like ``int i = 0``, `_try_algebraic_simplify` then replaced the
        live `i` in the body with the stale literal 0, folding ``i + 1`` to the
        constant `1` on every iteration (miscompile: `test_vector` stored value
        `1` for all 100000 pushes).  Return the saved tables so the caller can
        restore them after the loop, dropping any names the loop may have
        changed."""

        class _ForBody:
            __slots__ = ("body",)

            def __init__(self, body):
                self.body = body

        mut = set(_loop_written_names(_ForBody(node["body"]))) | {var_name}
        saved = self._const_prop
        active = dict(saved)
        for name in mut:
            active.pop(name, None)
        self._const_prop = active
        saved_f = self._const_prop_f
        active_f = dict(saved_f)
        for name in mut:
            active_f.pop(name, None)
        self._const_prop_f = active_f
        return saved, saved_f, mut

    def _exit_for_const_prop(self, saved, saved_f, mut):
        for name in mut:
            saved.pop(name, None)
            saved_f.pop(name, None)
        self._const_prop = saved
        self._const_prop_f = saved_f

    def _emit_for_worker(self, node, var_name, iterable, body, iter_type, var_type):

        # ---- 3.  Bounded loop unrolling for constant-size list literals ----
        if (
            isinstance(iterable, ListLit)
            and len(iterable.items) <= 16
            and var_type != "dynamic"
            and iter_type != "dynamic"
        ):
            # Guard: body must not contain break/continue/return.
            legit = True
            for stmt in body:
                if isinstance(stmt, (Break, Continue, Return, Raise)):
                    legit = False
                    break
                if isinstance(stmt, (If, While)):
                    legit = False
                    break
            if legit and not self.debug_instrument:
                var_ty = self.llvm_type(var_type)
                self._push_scope()
                var_ptr = self._alloca(var_ty, name=var_name)
                self._declare_local(var_name, var_ptr, var_type)
                for item in iterable.items:
                    self.builder.store(self.emit(item), var_ptr)
                    for stmt in body:
                        if not self._block_terminated():
                            self.emit(stmt)
                self._pop_scope()
                return

        iter_ptr = self.emit(iterable)

        if iter_type == "dynamic" and not self.no_userspace:
            return self._emit_for_dynamic(node, var_name, iterable, body)

        if iter_type.endswith("[]") and not self.no_userspace:
            return self._emit_for_array(node, var_name, iter_ptr, var_type, body)

        char_ptr_ty = ir.PointerType(_i8)
        if iter_ptr.type != char_ptr_ty:
            iter_ptr = self.builder.bitcast(iter_ptr, char_ptr_ty)

        len_fn = self._get_strlen_fn()
        length = self.builder.call(len_fn, [iter_ptr])
        if length.type.width != 32:
            length = self.builder.trunc(length, _i32)

        idx_ptr = self._alloca(_i32, name=f"{var_name}.idx")
        self.builder.store(ir.Constant(_i32, 0), idx_ptr)

        self._push_scope()
        var_ptr = self._alloca(_i8, name=var_name)
        self._declare_local(var_name, var_ptr, "char")

        cond_bb = self.builder.append_basic_block(f"for.{var_name}.cond")
        body_bb = self.builder.append_basic_block(f"for.{var_name}.body")
        inc_bb = self.builder.append_basic_block(f"for.{var_name}.inc")
        end_bb = self.builder.append_basic_block(f"for.{var_name}.end")

        self.builder.branch(cond_bb)
        self.builder.position_at_end(cond_bb)
        self._dbg_loop_head(node)

        idx = self.builder.load(idx_ptr)
        cmp = self.builder.icmp_signed("<", idx, length)
        self.builder.cbranch(cmp, body_bb, end_bb)

        self.builder.position_at_end(body_bb)
        idx_body = self.builder.load(idx_ptr)
        char_ptr = self.builder.gep(iter_ptr, [idx_body], inbounds=True)
        char_val = self.builder.load(char_ptr)
        self.builder.store(char_val, var_ptr)
        self.loop_stack.append((inc_bb, end_bb))
        for stmt in body:
            if not self._block_terminated():
                self.emit(stmt)
        self.loop_stack.pop()

        if not self._block_terminated():
            self.builder.branch(inc_bb)

        self.builder.position_at_end(inc_bb)
        idx_inc = self.builder.load(idx_ptr)
        idx_next = self.builder.add(idx_inc, ir.Constant(_i32, 1))
        self.builder.store(idx_next, idx_ptr)
        self.builder.branch(cond_bb)

        self.builder.position_at_end(end_bb)
        self._pop_scope()

    def _emit_for_dynamic(self, node, var_name, iterable, body):
        k, b = self._dyn_pair(iterable)
        arr = self.builder.inttoptr(b, _DynValuePtr)
        arr8 = self.builder.bitcast(arr, _i8ptr)
        length = self.builder.call(self.functions["cpyte_array_len"], [arr8])
        is_list = self.builder.icmp_unsigned("==", k, ir.Constant(_i32, _DYN_LIST))
        length = self.builder.select(is_list, length, ir.Constant(_i64, 0))

        idx_ptr = self._alloca(_i64, name=f"{var_name}.idx")
        self.builder.store(ir.Constant(_i64, 0), idx_ptr)

        self._push_scope()
        var_ptr = self._dyn_local_ptr(var_name)

        cond_bb = self.builder.append_basic_block(f"for.{var_name}.cond")
        body_bb = self.builder.append_basic_block(f"for.{var_name}.body")
        inc_bb = self.builder.append_basic_block(f"for.{var_name}.inc")
        end_bb = self.builder.append_basic_block(f"for.{var_name}.end")

        self.builder.branch(cond_bb)
        self.builder.position_at_end(cond_bb)
        self._dbg_loop_head(node)

        idx = self.builder.load(idx_ptr)
        cmp = self.builder.icmp_signed("<", idx, length)
        self.builder.cbranch(cmp, body_bb, end_bb)

        self.builder.position_at_end(body_bb)
        idx_body = self.builder.load(idx_ptr)
        elem_ptr = self.builder.gep(arr, [idx_body], inbounds=True)
        elem_val = self.builder.load(elem_ptr)
        self.builder.store(elem_val, var_ptr)

        self.loop_stack.append((inc_bb, end_bb))
        for stmt in body:
            if not self._block_terminated():
                self.emit(stmt)
        self.loop_stack.pop()

        if not self._block_terminated():
            self.builder.branch(inc_bb)

        self.builder.position_at_end(inc_bb)
        idx_inc = self.builder.load(idx_ptr)
        idx_next = self.builder.add(idx_inc, ir.Constant(_i64, 1))
        self.builder.store(idx_next, idx_ptr)
        self.builder.branch(cond_bb)

        self.builder.position_at_end(end_bb)
        self._pop_scope()

    def _emit_for_array(self, node, var_name, arr_ptr, var_type, body):
        if not isinstance(arr_ptr.type, ir.PointerType):
            raise Exception(f"cannot iterate non-pointer value of type {arr_ptr.type}")

        arr8 = arr_ptr
        if arr8.type != _i8ptr:
            arr8 = self.builder.bitcast(arr8, _i8ptr)
        len_fn = self.functions["cpyte_array_len"]
        length = self.builder.call(len_fn, [arr8])

        idx_ptr = self._alloca(_i64, name=f"{var_name}.idx")
        self.builder.store(ir.Constant(_i64, 0), idx_ptr)

        self._push_scope()
        var_ty = self.llvm_type(var_type)
        var_ptr = self._alloca(var_ty, name=var_name)
        self._declare_local(var_name, var_ptr, var_type)

        cond_bb = self.builder.append_basic_block(f"for.{var_name}.cond")
        body_bb = self.builder.append_basic_block(f"for.{var_name}.body")
        inc_bb = self.builder.append_basic_block(f"for.{var_name}.inc")
        end_bb = self.builder.append_basic_block(f"for.{var_name}.end")

        self.builder.branch(cond_bb)
        self.builder.position_at_end(cond_bb)
        self._dbg_loop_head(node)

        idx = self.builder.load(idx_ptr)
        cmp = self.builder.icmp_signed("<", idx, length)
        self.builder.cbranch(cmp, body_bb, end_bb)

        self.builder.position_at_end(body_bb)
        idx_body = self.builder.load(idx_ptr)
        elem_ptr = self.builder.gep(arr_ptr, [idx_body], inbounds=True)
        elem_val = self.builder.load(elem_ptr)
        self.builder.store(elem_val, var_ptr)

        self.loop_stack.append((inc_bb, end_bb))
        for stmt in body:
            if not self._block_terminated():
                self.emit(stmt)
        self.loop_stack.pop()

        if not self._block_terminated():
            self.builder.branch(inc_bb)

        self.builder.position_at_end(inc_bb)
        idx_inc = self.builder.load(idx_ptr)
        idx_next = self.builder.add(idx_inc, ir.Constant(_i64, 1))
        self.builder.store(idx_next, idx_ptr)
        self.builder.branch(cond_bb)

        self.builder.position_at_end(end_bb)
        self._pop_scope()

    @register_emitter(Number)
    def emit_number(self, node):
        if getattr(node, "is_bool", False):
            return ir.Constant(ir.IntType(1), 1 if node.value == "1" else 0)
        if getattr(node, "inferred_type", "") in ("big", "ubig"):
            s = node.value
            if s.startswith("0x") or s.startswith("0X"):
                s = str(int(s, 16))
            key = s + "\0"
            g = self.biglit_pool.get(key)
            if g is None:
                arr_ty = ir.ArrayType(_i8, len(key))
                g = ir.GlobalVariable(
                    self.module, arr_ty, f".biglit.{len(self.biglit_pool)}"
                )
                g.initializer = ir.Constant(arr_ty, bytearray(key.encode()))  # type: ignore[attr-defined]
                g.global_constant = True
                self.biglit_pool[key] = g
            ptr = self.builder.bitcast(g, _i8ptr)
            return self.builder.call(self.functions["bigint_from_str"], [ptr])

        # Handle hexadecimal literals BEFORE the float 'e' check: hex values
        # legitimately contain the letter 'e' as a digit (0-9a-f).
        if node.value.startswith("0x") or node.value.startswith("0X"):
            value = int(node.value, 16)
            if value > 2**31 - 1 or value < -(2**31):
                if value > 0 and value > 2**63 - 1:
                    return ir.Constant(ir.IntType(64), value - 2**64)
                return ir.Constant(ir.IntType(64), value)
            return ir.Constant(ir.IntType(32), value)

        if "." in node.value or "e" in node.value.lower():
            return ir.Constant(ir.DoubleType(), float(node.value))

        value = int(node.value)
        # Use 64-bit for large decimal values
        if value > 2**31 - 1 or value < -(2**31):
            return ir.Constant(ir.IntType(64), value)
        return ir.Constant(ir.IntType(32), value)

    @register_emitter(String)
    def emit_string(self, node):
        return self._string_const(node.value)

    def _string_const(self, val: str):
        val = val + "\0"
        if val not in self.string_pool:
            arr_ty = ir.ArrayType(ir.IntType(8), len(val))
            init = ir.Constant(arr_ty, bytearray(val.encode("utf-8")))
            name = f".str.{self.string_id}"
            self.string_id += 1
            gv = ir.GlobalVariable(self.module, arr_ty, name=name)
            gv.global_constant = True
            gv.initializer = init  # type: ignore[attr-defined]
            self.string_pool[val] = gv
        else:
            gv = self.string_pool[val]
        zero = ir.Constant(ir.IntType(32), 0)
        return self.builder.gep(gv, [zero, zero], inbounds=True)

    def _extend_to_i64(self, value):
        if isinstance(value.type, ir.IntType) and value.type.width < 64:
            if value.type.width == 32:
                return self.builder.sext(value, ir.IntType(64))
            return self.builder.zext(value, ir.IntType(64))
        if isinstance(value.type, (ir.FloatType, ir.DoubleType)):
            return self.builder.fptosi(value, ir.IntType(64))
        return value

    def _alloca(self, ty, name=""):
        if isinstance(ty, ir.VoidType):
            self._codegen_error(
                "cannot allocate variable of type `void` (use `void*` for a pointer to void)"
            )
        entry_block = self.builder.function.entry_basic_block
        saved_block = self.builder.block
        self.builder.position_at_start(entry_block)
        result = self.builder.alloca(ty, name=name)
        self.builder.position_at_end(saved_block)
        return result

    def _push_scope(self):
        """Enter a lexical scope; inner declarations shadow (not clobber) outer ones."""
        self.scope_stack.append({})

    def _pop_scope(self):
        """Leave a lexical scope, restoring any bindings shadowed inside it."""
        saved = self.scope_stack.pop()
        for name, state in saved.items():
            prev_local, prev_type, prev_ssa, prev_ssa_type, prev_const = state
            if prev_local is None:
                self.locals.pop(name, None)
            else:
                self.locals[name] = prev_local
            if prev_type is None:
                self.local_types.pop(name, None)
            else:
                self.local_types[name] = prev_type
            if prev_ssa is None:
                self.ssa_values.pop(name, None)
            else:
                self.ssa_values[name] = prev_ssa
            if prev_ssa_type is None:
                self.ssa_types.pop(name, None)
            else:
                self.ssa_types[name] = prev_ssa_type
            if prev_const is None:
                self.const_vars.pop(name, None)
            else:
                self.const_vars[name] = prev_const

    def _declare_local(self, name, ptr, ty):
        """Bind a variable in the current scope, remembering any outer binding."""
        state = (
            self.locals.get(name),
            self.local_types.get(name),
            self.ssa_values.get(name),
            self.ssa_types.get(name),
            self.const_vars.get(name),
        )
        self.locals[name] = ptr
        self.local_types[name] = ty
        self.ssa_values.pop(name, None)
        self.ssa_types.pop(name, None)
        if self.scope_stack:
            frame = self.scope_stack[-1]
            if name not in frame:
                frame[name] = state

    def _declare_const(self, name, value):
        """Bind a compile-time constant in the current scope, remembering any outer binding."""
        state = (
            self.locals.get(name),
            self.local_types.get(name),
            self.ssa_values.get(name),
            self.ssa_types.get(name),
            self.const_vars.get(name),
        )
        self.const_vars[name] = value
        self.locals.pop(name, None)
        self.local_types.pop(name, None)
        self.ssa_values.pop(name, None)
        self.ssa_types.pop(name, None)
        if self.scope_stack:
            frame = self.scope_stack[-1]
            if name not in frame:
                frame[name] = state

    @register_emitter(VarDecl)
    def emit_vardecl(self, node):
        if getattr(node, "dynamic", False) and not self.no_userspace:
            if node.init:
                value = self.emit(node.init)
                self._emit_dyn_store(
                    node.name,
                    value,
                    getattr(node.init, "inferred_type", None) or node.var_type,
                )
            else:
                out = self.builder.insert_value(
                    ir.Constant(_DynValue, ir.Undefined),
                    ir.Constant(_i32, _DYN_NONE),
                    0,
                )
                out = self.builder.insert_value(out, ir.Constant(_i64, 0), 1)
                self.builder.store(out, self._dyn_local_ptr(node.name))
                self.ssa_values.pop(node.name, None)
                self.ssa_types.pop(node.name, None)
            return
        ty = self.llvm_type(node.var_type)
        if isinstance(ty, ir.VoidType):
            self._codegen_error(
                f"cannot declare variable `{node.name}` of type `void` "
                f"(use `void*` for a pointer to void)",
                node,
            )
        if self.no_userspace and node.var_type == "dynamic":
            ty = _i32
        if node.is_const:
            if node.init:
                value = self.emit(node.init)
                if node.var_type in ("big", "ubig") and not self._is_biglike(node.init):
                    src_t = getattr(node.init, "inferred_type", None)
                    value = (
                        self._promote_to_ubig(value)
                        if node.var_type == "ubig"
                        else self._promote_to_big(value, src_t)
                    )
                elif node.var_type in ("int64", "uint64"):
                    value = self._extend_to_i64(value)
                if isinstance(value, ir.Constant):
                    self._declare_const(node.name, value)
                else:
                    ptr = self._alloca(ty, name=node.name)
                    self.builder.store(value, ptr)
                    self._declare_const(node.name, ptr)
            else:
                self._declare_const(node.name, ir.Constant(ty, 0))
            return
        ptr = self._alloca(ty, name=node.name)
        self._declare_local(node.name, ptr, _bc_array_norm(node.var_type))
        if node.init:
            if not self.no_userspace and self._is_dynamic_expr(node.init):
                value = self._unbox_dyn_to(node.init, node.var_type)
            else:
                value = self.emit(node.init)
            if node.var_type in ("big", "ubig") and not self._is_biglike(node.init):
                src_t = getattr(node.init, "inferred_type", None)
                value = (
                    self._promote_to_ubig(value)
                    if node.var_type == "ubig"
                    else self._promote_to_big(value, src_t)
                )
            elif self._is_biglike(node.init) and node.var_type not in ("big", "ubig"):
                pass
            elif node.var_type in ("int64", "uint64"):
                value = self._extend_to_i64(value)
            if value.type != ty:
                if self.no_userspace and value.type == _DynValue:
                    raise Exception(
                        "`dynamic` values require the userspace runtime; "
                        "not available under --no-userspace"
                    )
                if isinstance(value.type, ir.IntType) and isinstance(
                    ty, ir.PointerType
                ):
                    if self._is_i8_to_str(value, ty):
                        value = self._char_to_str(value)
                    else:
                        i64_ty = ir.IntType(64)
                        if value.type.width < 64:
                            value = self.builder.zext(value, i64_ty)
                        value = self.builder.inttoptr(value, ty)
                elif isinstance(value.type, ir.IntType) and isinstance(ty, ir.IntType):
                    if value.type.width < ty.width:
                        value = self.builder.zext(value, ty)
                    elif value.type.width > ty.width:
                        value = self.builder.trunc(value, ty)
                elif isinstance(value.type, ir.PointerType) and isinstance(
                    ty, ir.IntType
                ):
                    value = self.builder.load(value)
                    if value.type != ty:
                        value = self.builder.trunc(value, ty)
                elif isinstance(value.type, ir.PointerType) and isinstance(
                    ty, ir.PointerType
                ):
                    value = self.builder.bitcast(value, ty)
                elif isinstance(value.type, ir.IntType) and isinstance(
                    ty, (ir.FloatType, ir.DoubleType)
                ):
                    value = self.builder.sitofp(value, ty)
                elif isinstance(
                    value.type, (ir.FloatType, ir.DoubleType)
                ) and isinstance(ty, ir.PointerType):
                    i64_ty = ir.IntType(64)
                    int_val = self.builder.fptosi(value, i64_ty)
                    value = self.builder.inttoptr(int_val, ty)
                elif isinstance(
                    value.type, (ir.FloatType, ir.DoubleType)
                ) and isinstance(ty, ir.IntType):
                    value = self.builder.fptosi(value, ty)
            self.builder.store(value, ptr)
            self._set_const_prop(node.name, _const_int_value(node.init))
            if _const_int_value(node.init) is None and node.var_type in (
                "float",
                "double",
            ):
                fv = _const_fp_value(node.init)
                if fv is not None:
                    self._const_prop_f[node.name] = fv
                else:
                    self._const_prop_f.pop(node.name, None)
            else:
                self._const_prop_f.pop(node.name, None)
        elif isinstance(ty, ir.PointerType):
            self.builder.store(ir.Constant(ty, None), ptr)
        elif isinstance(ty, (ir.IntType, ir.FloatType, ir.DoubleType)):
            self.builder.store(ir.Constant(ty, 0), ptr)

    @register_emitter(Import)
    def emit_import(self, node):
        var_names = getattr(node, "var_names", set()) or set()
        for fname, (ret_type, params, vararg) in node.symbols:
            if fname in self.functions or fname in self.global_vars:
                continue
            if fname in var_names:
                # Variable declaration (e.g., CF_EXPORT const ...)
                var_ty = self.llvm_type(ret_type)
                if isinstance(var_ty, ir.VoidType):
                    continue
                gv = ir.GlobalVariable(self.module, var_ty, name=fname)
                gv.linkage = "extern_weak"
                self.global_vars[fname] = gv
            else:
                ret_ty = self.llvm_type(ret_type)
                if isinstance(ret_ty, ir.VoidType) and not params and not vararg:
                    param_tys = []
                else:
                    param_tys = [self.llvm_type(t) for _, t in params]
                fnty = ir.FunctionType(ret_ty, param_tys, var_arg=vararg)
                func = ir.Function(self.module, fnty, name=fname)
                self.functions[fname] = func
        if node.src_file:
            self.import_src_files.append(node.src_file)
        if getattr(node, "prebuilt_ll_files", None):
            self.import_src_files.extend(node.prebuilt_ll_files)
