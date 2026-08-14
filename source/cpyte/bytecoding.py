import hmac
import hashlib
from typing import Any, Protocol

from llvmlite import ir
from llvmlite.ir import instructions

from .lexar import Token, TokenType
from .astparse import *
from .extension_hooks import get_global_hook_registry
from .extension_hooks import HookLoadError


class _IRValue(Protocol):
    """Anything that carries an LLVM type."""
    type: Any

_i8 = ir.IntType(8)
_i32 = ir.IntType(32)
_i64 = ir.IntType(64)
_i1 = ir.IntType(1)
_double = ir.DoubleType()
_void = ir.VoidType()
_i8ptr = ir.PointerType(_i8)

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

    def call(self, fn, args, name='', cconv=None, tail=False, fastmath=(),
             attrs=(), arg_attrs=None):
        if self._unwind_to is not None and not isinstance(fn, ir.InlineAsm):
            cont = self.append_basic_block("invoke.cont")
            ret = self.invoke(fn, args, cont, self._unwind_to, name=name,
                              cconv=cconv, fastmath=fastmath, attrs=attrs,
                              arg_attrs=arg_attrs)
            self.position_at_end(cont)
            return ret
        return super().call(fn, args, name=name, cconv=cconv, tail=tail,
                            fastmath=fastmath, attrs=attrs, arg_attrs=arg_attrs)


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
    return []


class LLVM:
    def llvm_type(self, t: str):
        if t == 'int':
            return ir.IntType(32)
        if t == 'int64':
            return ir.IntType(64)
        if t == 'uint64':
            return ir.IntType(64)
        if t == 'bool':
            return ir.IntType(1)
        if t in ('float', 'double'):
            return ir.DoubleType()
        if t == 'void':
            return ir.VoidType()
        if t == 'str':
            return ir.PointerType(ir.IntType(8))
        if t == 'char':
            return ir.IntType(8)
        if t == 'void*':
            return ir.PointerType(ir.IntType(8))
        if t == 'big':
            return ir.PointerType(ir.IntType(8))
        if t.endswith('[]'):
            base = self.llvm_type(t[:-2])
            return ir.PointerType(base)
        if t.endswith('*'):
            base = self.llvm_type(t[:-1])
            return ir.PointerType(base)
        if t.endswith('&'):
            base = self.llvm_type(t[:-1])
            return ir.PointerType(base)
        if t in self.structs:
            return self.structs[t]
        # Handle generic types like Pair<int, string>
        if '<' in t:
            # Check if we have a monomorphized version
            if t in self.structs:
                return self.structs[t]
            # Try to generate it now
            resolved = self._resolve_generic_type(t)
            if resolved is not None:
                return resolved
            # Fallback: use base struct (will have wrong field types but at least won't crash)
            base_name = t.split('<')[0]
            if base_name in self.structs:
                return self.structs[base_name]
        return ir.IntType(32)

    def _base_type_name(self, t: str) -> str:
        while t.endswith('[]'):
            t = t[:-2]
        while t.endswith('*') or t.endswith('&'):
            t = t[:-1]
        if '"struct.' in t:
            start = t.index('"struct.') + len('"struct.')
            end = t.index('"', start)
            t = t[start:end]
        idx = t.find('<')
        if idx != -1:
            t = t[:idx]
        return t

    def _resolve_generic_type(self, type_str: str):
        """Resolve a generic type like Pair<int, string> to a monomorphized LLVM struct."""
        if '<' not in type_str:
            return None
        idx = type_str.index('<')
        base_name = type_str[:idx]
        args_str = type_str[idx+1:-1]  # strip < and >
        # Parse type args (handle nested generics)
        args = []
        depth = 0
        current = ''
        for ch in args_str:
            if ch == '<':
                depth += 1
                current += ch
            elif ch == '>':
                depth -= 1
                current += ch
            elif ch == ',' and depth == 0:
                args.append(current.strip())
                current = ''
            else:
                current += ch
        if current.strip():
            args.append(current.strip())
        # Look up the base struct
        base_struct_node = None
        for name, sym in [('struct', None)]:
            # We need the original AST node; store it during emit_struct
            pass
        # Check if we already emitted a specialized version
        spec_name = f'{base_name}__{"_".join(a.replace("<", "_").replace(">", "_").replace(",", "_").replace(" ", "") for a in args)}'
        if spec_name in self.structs:
            return self.structs[spec_name]
        # Look up the base struct definition
        if base_name not in self.structs:
            return None
        # Find the StructDef node for this struct
        struct_node = getattr(self, '_struct_nodes', {}).get(base_name)
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
        # Create the specialized struct
        llvm_struct = ir.IdentifiedStructType(ir.global_context, f"struct.{spec_name}")
        llvm_struct.set_body(*field_tys)
        self.structs[spec_name] = llvm_struct
        self.struct_fields[spec_name] = struct_node.fields
        return llvm_struct

    def _is_ir_constant_zero(self, val):
        if isinstance(val, ir.Constant) and val.constant == 0:
            return True
        if isinstance(val, ir.Constant) and isinstance(val.type, ir.PointerType) and val.constant is None:
            return True
        return False

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

    def _emit_int_divmod(self, left, right, is_rem):
        """Signed int division/remainder that stays well-defined in LLVM IR.

        cpy ints wrap, so `INT_MIN // -1` yields INT_MIN and `INT_MIN % -1`
        yields 0. Raw `sdiv`/`srem` on those operands is *poison* in LLVM and
        the optimizer folds it into arbitrary values (opt0 vs opt3 diverge).
        Division/remainder by zero still traps, matching the cpy runtime.
        """
        width = left.type.width if isinstance(left.type, ir.IntType) else 64
        int_min = -(1 << (width - 1))

        if isinstance(right, ir.Constant):
            if self._is_ir_constant_zero(right):
                # Division/remainder by a compile-time zero: fall through to the
                # runtime trap path below (same semantics as a runtime div-by-zero)
                # instead of aborting codegen mid-emission.
                pass
            elif isinstance(left, ir.Constant):
                l = self._norm_signed(left.constant, width)
                r = self._norm_signed(right.constant, width)
                if l == int_min and r == -1:
                    return ir.Constant(left.type, 0 if is_rem else int_min)
                if is_rem:
                    q = self._trunc_div(l, r)
                    return ir.Constant(left.type, l - q * r)
                return ir.Constant(left.type, self._trunc_div(l, r))

        is_zero = self.builder.icmp_signed('==', right, ir.Constant(right.type, 0))
        trap_bb = self.builder.append_basic_block("div.trap")
        guard_bb = self.builder.append_basic_block("div.guard")
        self.builder.cbranch(is_zero, trap_bb, guard_bb)
        self.builder.position_at_end(trap_bb)
        self.builder.call(self._get_trap_fn(), [])
        self.builder.unreachable()

        self.builder.position_at_end(guard_bb)
        is_min = self.builder.icmp_signed('==', left, ir.Constant(left.type, int_min))
        is_neg1 = self.builder.icmp_signed('==', right, ir.Constant(right.type, -1))
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


    def _clamp_shift_amount(self, val, bitwidth):
        zero = ir.Constant(val.type, 0)
        max_shift = ir.Constant(val.type, bitwidth - 1)
        lt_zero = self.builder.icmp_signed('<', val, zero)
        gt_max = self.builder.icmp_signed('>', val, max_shift)
        clamped_to_max = self.builder.select(gt_max, max_shift, val)
        return self.builder.select(lt_zero, zero, clamped_to_max)

    def _is_big(self, node):
        if getattr(node, 'inferred_type', '') == 'big':
            return True
        if isinstance(node, Variable):
            return self.local_types.get(node.name, '') == 'big'
        return False

    def _promote_to_big(self, val):
        if isinstance(val.type, ir.IntType) and val.type.width < 64:
            if val.type.width == 32:
                val = self.builder.sext(val, _i64)
            else:
                val = self.builder.zext(val, _i64)
        if isinstance(val.type, ir.IntType) and 'i64' in str(val.type):
            fn = self.functions['bigint_from_uint64']
        elif isinstance(val.type, ir.IntType):
            fn = self.functions['bigint_from_int']
        else:
            return val
        return self.builder.call(fn, [val])

    def __init__(self, no_userspace=False, enable_extensions=True, target_triple=None, no_gc=False, use_native_eh=False):
        self.module = ir.Module("main")
        self.no_gc = no_gc
        self.use_native_eh = use_native_eh
        self._target_triple = target_triple
        try:
            import llvmlite.binding as _binding
            _binding.initialize_native_target()
            if target_triple:
                _target = _binding.Target.from_triple(target_triple)
            else:
                _target = _binding.Target.from_default_triple()
            _tm = _target.create_target_machine()
            self.module.triple = _tm.triple
            self.module.data_layout = str(_tm.target_data)
        except Exception:
            pass
        self.builder: Any = None
        self.functions = {}
        self.global_vars = {}
        #Constant Variable store here.
        self.const_vars = {}
        self.locals = {}
        self.local_types = {}
        self.string_id = 0
        self.string_pool = {}
        self.biglit_pool = {}
        self.ssa_values = {}
        self.ssa_types = {}  # Track types of SSA values
        self.scope_stack = []
        self.structs = {}
        self.struct_fields = {}
        self.import_src_files = []
        self.loop_stack = []
        self._malloc_fn = None
        self._free_fn = None
        self._gc_alloc_fn = None
        self._gc_write_barrier_fn = None
        self._sizeof_cache = {}
        self._strlen_fn = None
        self._memcpy_fn = None
        self.no_userspace = no_userspace
        self.enable_extensions = enable_extensions
        self._hook_registry = get_global_hook_registry()
        self.generic_instantiations = {}  # name -> [(type_args_tuple, ...)]
        self._struct_nodes = {}  # name -> StructDef AST node (for generic resolution)
        self._emit_depth = 0
        self._emit_memo = {}
        self._in_emit_iterative = False
        
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
            ('bigint_new', _i8ptr, []),
            ('bigint_free', _void, [_i8ptr]),
            ('bigint_from_int', _i8ptr, [_i64]),
            ('bigint_from_uint64', _i8ptr, [_i64]),
            ('bigint_from_str', _i8ptr, [_i8ptr]),
            ('bigint_input', _i8ptr, []),
            ('bigint_add', _i8ptr, [_i8ptr, _i8ptr]),
            ('bigint_sub', _i8ptr, [_i8ptr, _i8ptr]),
            ('bigint_mul', _i8ptr, [_i8ptr, _i8ptr]),
            ('bigint_div', _i8ptr, [_i8ptr, _i8ptr]),
            ('bigint_mod', _i8ptr, [_i8ptr, _i8ptr]),
            ('bigint_neg', _i8ptr, [_i8ptr]),
            ('bigint_cmp', _i32, [_i8ptr, _i8ptr]),
            ('bigint_print', _void, [_i8ptr]),
            ('bigint_to_str', _i8ptr, [_i8ptr]),
        ]
        for name, ret, args in bignum_fns:
            fn = ir.Function(self.module, ir.FunctionType(ret, args), name=name)
            self.functions[name] = fn

        # Concurrent tri-color GC runtime functions (ugc-based)
        if not self.no_gc:
            gc_fns = [
                ('gc_init', _void, []),
                ('gc_malloc', _i8ptr, [_i64]),
                ('gc_write_barrier', _void, [_i8ptr, _i8ptr]),
                ('gc_collect', _void, []),
                ('gc_start_thread', _void, []),
                ('gc_stop_thread', _void, []),
                ('gc_shutdown', _void, []),
            ]
            for name, ret, args in gc_fns:
                fn = ir.Function(self.module, ir.FunctionType(ret, args), name=name)
                self.functions[name] = fn

        # Exception handling globals
        self._exc_type = ir.GlobalVariable(
            self.module,
            _i8ptr,
            "_exc_type"
        )
        self._exc_type.initializer = ir.Constant(_i8ptr, None)  # type: ignore[attr-defined]

        if not use_native_eh:
            self._exc_buf_ptr = ir.GlobalVariable(self.module, _i8ptr, "_exc_buf_ptr")
            self._exc_buf_ptr.initializer = ir.Constant(_i8ptr, None)  # type: ignore[attr-defined]
            setjmp_ty = ir.FunctionType(_i32, [_i8ptr])
            self._setjmp_fn = ir.Function(self.module, setjmp_ty, "setjmp")
            self._setjmp_fn.attributes.add('returns_twice')
            longjmp_ty = ir.FunctionType(_void, [_i8ptr, _i32])
            self._longjmp_fn = ir.Function(self.module, longjmp_ty, "longjmp")
            self._longjmp_fn.attributes.add('noreturn')
            # Define functions so dedup function can check duplications. (Error in examples/test.cpy)
            self.functions['setjmp'] = self._setjmp_fn
            self.functions['longjmp'] = self._longjmp_fn
        else:
            pers_ty = ir.FunctionType(_i32, [], var_arg=True)
            self._personality_fn = ir.Function(self.module, pers_ty, "cpy_personality")
            raise_ty = ir.FunctionType(_void, [_i8ptr, _i8ptr])
            self._raise_exception_fn = ir.Function(self.module, raise_ty, "cpy_raise_exception")
            resume_ty = ir.FunctionType(_void, [_i8ptr])
            self._resume_fn = ir.Function(self.module, resume_ty, "cpy_resume")
            # Defined for the same reason
            self.functions['resume'] = self._resume_fn
            self.functions['raise'] = self._raise_exception_fn
            self.functions['personality'] = self._personality_fn

        # strcmp for exception type matching
        strcmp_ty = ir.FunctionType(_i32, [_i8ptr, _i8ptr])
        if not self.module.scope.is_used('strcmp'):
            self._strcmp_fn = ir.Function(self.module, strcmp_ty, "strcmp")
        else:
            for g in self.module.globals:
                if isinstance(g, ir.Function) and g.name == 'strcmp':
                    self._strcmp_fn = g
                    break
        self.functions['strcmp'] = self._strcmp_fn

    @register_emitter(Switch)
    def emit_switch(self, node):
        value = self.emit(node.value)
        if not isinstance(value.type, ir.IntType):
            value = self._is_true(value)

        end_blk = self.builder.append_basic_block(name="sw_end")

        case_irs = []
        case_blks = [None] * len(node.cases)
        default_blk = end_blk

        for i, (case_val, body) in enumerate(node.cases):
            if case_val is None:
                default_blk = self.builder.append_basic_block(name="sw_default")
                case_irs.append(None)
                case_blks[i] = default_blk
            else:
                val_ir = self.emit(case_val)
                case_irs.append(val_ir)

        if any(not isinstance(c, ir.Constant) for c in case_irs if c is not None):
            body_blk = self.builder.append_basic_block(name="sw_entry")
            self.builder.branch(body_blk)
            self.builder.position_at_start(body_blk)
            for i, (case_val, body) in enumerate(node.cases):
                if case_val is not None:
                    val_ir = case_irs[i]
                    eq = self.builder.icmp_signed('==', value, val_ir)
                    then_blk = self.builder.append_basic_block(name="sw_case")
                    nxt_blk = self.builder.append_basic_block(name="sw_next")
                    self.builder.cbranch(eq, then_blk, nxt_blk)
                    self.builder.position_at_start(then_blk)
                    self._push_scope()
                    for stmt in body:
                        self.emit(stmt)
                    self._pop_scope()
                    if not self._block_terminated():
                        self.builder.branch(end_blk)
                    self.builder.position_at_start(nxt_blk)
                else:
                    self.builder.branch(default_blk)
                    self.builder.position_at_start(default_blk)
                    self._push_scope()
                    for stmt in body:
                        self.emit(stmt)
                    self._pop_scope()
                    if not self._block_terminated():
                        self.builder.branch(end_blk)
            self.builder.position_at_start(end_blk)
            return

        for i, (case_val, body) in enumerate(node.cases):
            if case_val is not None:
                blk = self.builder.append_basic_block(name="sw_case")
                case_blks[i] = blk

        body_blk = self.builder.append_basic_block(name="sw_body")
        self.builder.branch(body_blk)
        self.builder.position_at_start(body_blk)

        sw = self.builder.switch(value, default_blk)
        for i, (case_val, _) in enumerate(node.cases):
            if case_val is not None:
                sw.add_case(case_irs[i], case_blks[i])

        for i, (case_val, body) in enumerate(node.cases):
            blk = case_blks[i]
            self.builder.position_at_start(blk)
            self._push_scope()
            for stmt in body:
                self.emit(stmt)
            self._pop_scope()
            if not self._block_terminated():
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
        is_exception = self.builder.icmp_signed('!=', result, ir.Constant(_i32, 0))
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
                is_match = self.builder.icmp_signed('==', cmp_res, ir.Constant(_i32, 0))
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
                last_blk = next_blk
            else:
                self.builder.store(old_buf, self._exc_buf_ptr)
                for stmt in handler.body:
                    if not self._block_terminated():
                        self.emit(stmt)
                if not self._block_terminated():
                    self.builder.branch(after_blk)
                last_blk = self.builder.block

        if not self._block_terminated():
            self.builder.store(old_buf, self._exc_buf_ptr)
            self.builder.branch(after_blk)

        self.builder.position_at_end(after_blk)
        return None

    def _emit_try_native(self, node):
        fn = self.builder.function
        fn.attributes.add('uwtable')
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
                is_match = self.builder.icmp_signed('==', cmp_res, ir.Constant(_i32, 0))
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
        return None

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
        return None

    def _emit_raise_native(self, node):
        exc_type_str = self._string_const(node.exc_type)
        msg = self.emit(node.message)
        self.builder.call(self._raise_exception_fn, [exc_type_str, msg])
        self.builder.unreachable()
        return None

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
                    if getattr(node, 'sub_ast', None):
                        _collect_nodes(node.sub_ast)
                elif isinstance(node, FuncDef):
                    funcdefs.append(node)
                else:
                    toplevel.append(node)

        _collect_nodes(ast)
        
        # Collect runtime code from extension hooks
        if self.enable_extensions:
            import tempfile
            import os
            for hook in self._hook_registry.get_runtime_hooks():
                try:
                    runtime_code = hook.get_runtime_code()
                    if runtime_code:
                        fd, tmp_path = tempfile.mkstemp(suffix='.c', prefix='hook_runtime_')
                        with os.fdopen(fd, 'w') as f:
                            f.write(runtime_code)
                        self.import_src_files.append(tmp_path)
                except Exception:
                    pass

        for node in structs:
            if isinstance(node, ClassDef):
                continue
            st = ir.IdentifiedStructType(
                ir.global_context, f"struct.{node.name}"
            )
            ir.global_context.identified_types[f"struct.{node.name}"] = st
            self.structs[node.name] = st

        for node in structs:
            self.emit(node)



        for node in imports:
            self.emit(node)

        user_main = None
        for node in funcdefs:
            if getattr(node, 'name', None) == 'main':
                user_main = node
                break

        if toplevel and user_main is not None:
            user_main.body = toplevel + user_main.body
            toplevel = []

        wrapper_builder = None
        if toplevel:
            main = ir.Function(
                self.module, ir.FunctionType(ir.IntType(32), []), "main"
            )
            entry = main.append_basic_block("entry")
            wrapper_builder = ir.IRBuilder(entry)

        for node in funcdefs:
            self.emit(node)

        if wrapper_builder is not None:
            self.builder = wrapper_builder
            if not self.no_gc:
                # Initialize concurrent tri-color GC and start background thread
                gc_init_fn = self.functions.get('gc_init')
                if gc_init_fn:
                    self.builder.call(gc_init_fn, [])
                gc_start_fn = self.functions.get('gc_start_thread')
                if gc_start_fn:
                    self.builder.call(gc_start_fn, [])
            for node in toplevel:
                self.emit(node)
            if not self.no_gc:
                gc_shutdown_fn = self.functions.get('gc_shutdown')
                if gc_shutdown_fn:
                    self.builder.call(gc_shutdown_fn, [])
            self.builder.ret(ir.Constant(ir.IntType(32), 0))

        return self.module, self.import_src_files

    @register_emitter(FString)
    def emit_fstring(self, node):
        parts = node.parts
        strings = []
        for kind, payload in parts:
            if kind == 'lit':
                strings.append(self._string_const(payload))
            else:
                val = self.emit(payload)
                strings.append(self._stringify_value(payload, val))
        result = strings[0] if strings else self._string_const('')
        for s in strings[1:]:
            result = self._concat_strings(result, s)
        return result

    def _stringify_value(self, node, val):
        """Convert a runtime value into a heap string (i8*) for interpolation."""
        if getattr(node, 'inferred_type', None) == 'str':
            return val
        if isinstance(node, Number) and (node.value.startswith('0x') or node.value.startswith('0X')):
            as_i64 = self._extend_to_i64(val)
            return self.builder.call(self.functions['str_of_ptr'], [as_i64])
        if getattr(node, 'inferred_type', None) == 'big':
            return self.builder.call(self.functions['bigint_to_str'], [val])
        ty = val.type
        if isinstance(ty, ir.PointerType):
            as_i64 = self.builder.ptrtoint(val, _i64)
            return self.builder.call(self.functions['str_of_ptr'], [as_i64])
        if isinstance(ty, ir.DoubleType):
            return self.builder.call(self.functions['str_of_double'], [val])
        if isinstance(ty, ir.FloatType):
            as_double = self.builder.fpext(val, ir.DoubleType())
            return self.builder.call(self.functions['str_of_double'], [as_double])
        if isinstance(ty, ir.IntType):
            i64 = self._extend_to_i64(val)
            if getattr(node, 'inferred_type', None) == 'uint64':
                return self.builder.call(self.functions['str_of_uint64'], [i64])
            return self.builder.call(self.functions['str_of_int64'], [i64])
        as_i64 = self.builder.ptrtoint(val, _i64)
        return self.builder.call(self.functions['str_of_ptr'], [as_i64])

    def _concat_strings(self, left, right):
        """Concatenate two heap strings (i8*), returning a new heap string."""
        strlen_fn = self._get_strlen_fn()
        malloc_fn = self._get_malloc_fn()
        memcpy_fn = self._get_memcpy_fn()
        left_len = self.builder.call(strlen_fn, [left])
        right_len = self.builder.call(strlen_fn, [right])
        total_len = self.builder.add(left_len, right_len)
        plus_one = self.builder.add(total_len, ir.Constant(_i32, 1))
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

    def emit(self, node: list[Token]) -> _IRValue:
        key = id(node)
        if key in self._emit_memo:
            return self._emit_memo[key]
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
        Number, String, Variable, Signed67, Input, InputStr, InputBig, SizeOf,
        UnaryOp, Deref, AddrOf, Index, Attr, NewExpr, Call, InlineAsm,
        ExprStmt,
    )

    def _emit_is_pure(self, node) -> bool:
        if isinstance(node, BinOp):
            return node.op not in (TokenType.AND, TokenType.OR)
        return isinstance(node, self._EMIT_PURE_TYPES)

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
        lhs_true = self._is_true(lhs)
        entry_bb = self.builder.block
        suffix = 'and' if op == TokenType.AND else 'or'
        rhs_bb = self.builder.append_basic_block(f'{suffix}.rhs')
        end_bb = self.builder.append_basic_block(f'{suffix}.end')
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
        rhs_true = self._is_true(rhs)
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
            stack = [('visit', node)]
            while stack:
                kind, n = stack.pop()
                key = id(n)
                if key in self._emit_memo:
                    continue
                if isinstance(n, BinOp) and n.op in (TokenType.AND, TokenType.OR):
                    self._emit_and_or_chain(n)
                    continue
                if self._emit_is_pure(n):
                    if kind == 'visit':
                        children = _emit_children(n)
                        if children:
                            stack.append(('combine', n))
                            for c in reversed(children):
                                stack.append(('visit', c))
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

    def _emit_recursive(self, node: list[Token]) -> _IRValue:
        # Try codegen hooks if extensions are enabled
        if self.enable_extensions:
            for hook in self._hook_registry.get_codegen_hooks():
                try:
                    if hook.should_emit_node(node):
                        return hook.emit_node(node, self.builder, {
                            'llvm': self,
                            'module': self.module,
                            'builder': self.builder,
                        })
                except Exception as e:
                    raise HookLoadError(
                        f"codegen hook {hook.__class__.__name__} failed: {e}"
                    ) from e

        if isinstance(node, dict):
            if node.get('type') == 'for':
                return self.emit_for(node)  # type: ignore[return]
            raise RuntimeError(
                f"emit: unsupported dict node type {node.get('type')!r}"
            )

        # Registry-based visitor (primary dispatch)
        handler = _EMIT_REGISTRY.get(type(node))
        if handler is not None:
            return handler(self, node)

        # Fallback: dynamic dispatch by naming convention
        method = getattr(
            self,
            f"emit_{type(node).__name__.lower()}",
            None
        )
        if method is not None:
            return method(node)

        raise RuntimeError(
            f"emit: no emitter registered for {type(node).__name__}"
        )

    @register_emitter(StructDef)
    def emit_structdef(self, node: StructDef):
        self._struct_nodes[node.name] = node
        if node.generic_params:
            # For generic structs, don't emit the base version with raw type params.
            # Specialized versions are generated on demand by _resolve_generic_type.
            # Still register it so lookup works.
            return
        field_tys = []
        for f in node.fields:
            field_tys.append(self.llvm_type(f.type_expr))
        llvm_struct = self.structs.get(node.name)
        if llvm_struct is not None and isinstance(llvm_struct, ir.IdentifiedStructType):
            llvm_struct.set_body(*field_tys)
        else:
            llvm_struct = ir.LiteralStructType(field_tys)
            llvm_struct.name = f"struct.{node.name}"  # type: ignore[attr-defined]
            self.structs[node.name] = llvm_struct
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
        if field_tys:
            llvm_struct = ir.IdentifiedStructType(ir.global_context, f"class.{node.name}")
            llvm_struct.set_body(*field_tys)
            self.structs[node.name] = llvm_struct
            self.struct_fields[node.name] = all_fields
        # Emit methods — each gets a hidden 'this' pointer as first parameter
        for m in node.methods:
            self._emit_class_method(node, m, all_fields)
                # Emit inherited methods (call through to base class implementation)
        if node.base:
            for fname, fobj in list(self.functions.items()):
                if fname.startswith(node.base + '.'):
                    method_name = fname[len(node.base) + 1:]
                    child_name = f'{node.name}.{method_name}'
                    if child_name not in self.functions:
                        # Create a wrapper that bitcasts the child pointer to base type
                        base_fnty = fobj.function_type
                        child_fnty = ir.FunctionType(base_fnty.return_type, [ir.PointerType(self.structs[node.name])] + list(base_fnty.args[1:]))
                        wrapper = ir.Function(self.module, child_fnty, child_name)
                        entry = wrapper.append_basic_block('entry')
                        wb = ir.IRBuilder(entry)
                        casted = wb.bitcast(wrapper.args[0], base_fnty.args[0])
                        args = [casted] + list(wrapper.args[1:])
                        ret = wb.call(fobj, args)
                        if base_fnty.return_type == ir.VoidType():
                            wb.ret_void()
                        else:
                            wb.ret(ret)
                        self.functions[child_name] = wrapper

    def _emit_class_method(self, class_node: ClassDef, method: FuncDef, all_fields: list):
        """Emit a class method with a hidden 'this' pointer."""
        # Build param types: this* + declared params
        class_ty = self.structs.get(class_node.name)
        if class_ty is None:
            return
        this_ty = ir.PointerType(class_ty)
        non_this_params = {k: v for k, v in method.params.items() if k != 'this'}
        param_tys = [this_ty] + [self.llvm_type(t) for t in non_this_params.values()]
        ret_ty = self.llvm_type(method.rettype or 'void')
        func_name = f'{class_node.name}.{method.name}'
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
        this_ptr = self.builder.alloca(this_ty, name='this')
        self.builder.store(this_arg, this_ptr)
        self.locals['this'] = this_ptr
        self.local_types['this'] = class_node.name + '*'
        # Store method params
        for llvm_arg, (pname, ptype) in zip(func.args[1:], non_this_params.items()):
            ptr = self.builder.alloca(llvm_arg.type, name=pname)
            self.builder.store(llvm_arg, ptr)
            self.locals[pname] = ptr
            self.local_types[pname] = ptype
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
        if node.type_expr == 'str':
            malloc_fn = self._get_malloc_fn()
            empty_str = self._string_const('')
            ptr = self.builder.call(malloc_fn, [ir.Constant(_i64, 8)])
            i8pp = ir.PointerType(_i8ptr)
            ptr = self.builder.bitcast(ptr, i8pp)
            self.builder.store(empty_str, ptr)
            self._emit_write_barrier(ptr, empty_str)
            return ptr
        # Resolve generic types
        type_str = node.type_expr
        if '<' in type_str and type_str not in self.structs:
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
        if node.type_expr.endswith('[]'):
            elem_ty = self.llvm_type(node.type_expr[:-2])
        malloc_ty = ir.PointerType(elem_ty)
        elem_size = self._sizeof_type(elem_ty)
        if elem_size.type != count.type:
            elem_size = self.builder.zext(elem_size, count.type)
        total_size = self.builder.mul(count, elem_size)
        malloc_fn = self._get_malloc_fn()
        ptr = self.builder.call(malloc_fn, [total_size])
        ptr = self.builder.bitcast(ptr, malloc_ty)
        return ptr

    def _get_malloc_fn(self):
        if self.no_gc:
            # Bare-metal: use plain malloc
            fn = self._malloc_fn
            if fn is not None:
                return fn
            for f in self.module.functions:
                if f.name == 'malloc':
                    self._malloc_fn = f
                    return f
            fnty = ir.FunctionType(_i8ptr, [_i64])
            fn = ir.Function(self.module, fnty, 'malloc')
            self._malloc_fn = fn
            return fn
        # Concurrent tri-color GC allocator
        fn = self._gc_alloc_fn
        if fn is not None:
            return fn
        for f in self.module.functions:
            if f.name == 'gc_malloc':
                self._gc_alloc_fn = f
                return f
        fnty = ir.FunctionType(_i8ptr, [_i64])
        fn = ir.Function(self.module, fnty, 'gc_malloc')
        self._gc_alloc_fn = fn
        return fn

    def _get_write_barrier_fn(self):
        # Get the gc_write_barrier function for pointer stores between heap objects
        fn = self._gc_write_barrier_fn
        if fn is not None:
            return fn
        for f in self.module.functions:
            if f.name == 'gc_write_barrier':
                self._gc_write_barrier_fn = f
                return f
        fnty = ir.FunctionType(_void, [_i8ptr, _i8ptr])
        fn = ir.Function(self.module, fnty, 'gc_write_barrier')
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
            if f.name == 'llvm.trap':
                return f
        fnty = ir.FunctionType(ir.VoidType(), [])
        fn = ir.Function(self.module, fnty, 'llvm.trap')
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
            if hasattr(ty, 'elements') and ty.elements is not None:
                max_align = 0
                total = 0
                for el in ty.elements:
                    el_sz, el_align = self._type_abi_info(el)
                    if el_align > max_align:
                        max_align = el_align
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

    @register_emitter(SizeOf)
    def emit_sizeof(self, node: SizeOf):
        ty = self.llvm_type(node.type_expr)
        return self._sizeof_type(ty)

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
            constraint_parts.append(','.join(out_constraints))
        if in_constraints:
            constraint_parts.append(','.join(in_constraints))
        if clobbers:
            constraint_parts.append(','.join('~' + c for c in clobbers))
        asm_constraints = ','.join(p for p in constraint_parts if p)
        asm_fn_type = ir.FunctionType(ret_ty, arg_tys)
        asm_fn = ir.InlineAsm(asm_fn_type, node.template, asm_constraints, side_effect=node.volatile)
        call = self.builder.call(asm_fn, arg_vals)
        if node.outputs and isinstance(node.outputs[0][1], Variable):
            ptr = self._emit_lvalue(node.outputs[0][1])
            self.builder.store(call, ptr)
        return call

    @register_emitter(Index)
    def emit_index(self, node: Index):
        ptr = self._emit_lvalue(node)
        return self.builder.load(ptr)

    def _emit_lvalue_index(self, node: Index):
        obj = self.emit(node.obj)
        idx = self.emit(node.index)
        if isinstance(idx.type, ir.PointerType):
            idx = self.builder.ptrtoint(idx, _i64)
        elif isinstance(idx.type, (ir.DoubleType, ir.FloatType)):
            idx = self.builder.fptosi(idx, _i64)
        return self.builder.gep(obj, [idx], inbounds=True)

    @register_emitter(Attr)
    def emit_attr(self, node: Attr):
        enum_val = getattr(node, '_enum_member_value', None)
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
        if struct_type and hasattr(struct_type, 'elements') and struct_type.elements is not None:
            elem_tys = struct_type.elements
            if field_idx < len(elem_tys):
                return self._named_type_repr(elem_tys[field_idx])
        return None

    def _named_type_repr(self, ty):
        if not hasattr(self, '_type_names'):
            self._type_names = {}
        if self._type_names:
            return self._type_names.get(ty)
        for name, t in self._iter_named_types():
            self._type_names[t] = name
        return self._type_names.get(ty)

    def _iter_named_types(self):
        yield 'int', ir.IntType(32)
        yield 'double', ir.DoubleType()
        yield 'str', ir.PointerType(ir.IntType(8))
        yield 'bool', ir.IntType(1)
        yield 'char', ir.IntType(8)
        for name in self.structs:
            yield name, self.structs[name]
        for name in self.structs:
            yield name + '*', ir.PointerType(self.structs[name])

    def _struct_name_from_node(self, node) -> str | None:
        if isinstance(node, Variable):
            declared = self.local_types.get(node.name, '')
            if declared:
                return self._base_type_name(declared)
        if isinstance(node, Deref):
            if isinstance(node.operand, Variable):
                declared = self.local_types.get(node.operand.name, '')
                if declared.endswith('*'):
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
            return self._struct_name_from_node(node.obj)
        return None

    def _emit_lvalue_attr(self, node: Attr):
        obj_ptr = self._emit_lvalue(node.obj)
        struct_name = self._struct_name_from_node(node.obj)
        if struct_name:
            # Handle generic struct names
            if '<' in struct_name and struct_name not in self.struct_fields:
                self._resolve_generic_type(struct_name)
            # Try the full generic name first, then base name
            for sn in (struct_name, struct_name.split('<')[0]):
                if sn and sn in self.struct_fields:
                    if isinstance(getattr(obj_ptr.type, 'pointee', None), ir.PointerType):
                        obj_ptr = self.builder.load(obj_ptr)
                    fields = self.struct_fields[sn]
                    for i, f in enumerate(fields):
                        if f.name == node.name:
                            return self.builder.gep(obj_ptr, [ir.Constant(_i32, 0), ir.Constant(_i32, i)], inbounds=True)
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
        ret_ty = self.llvm_type(node.rettype or 'int')
        param_tys = [self.llvm_type(t) for t in node.params.values()]

        if node.name in self.functions:
            func = self.functions[node.name]
        else:
            func = ir.Function(
                self.module,
                ir.FunctionType(ret_ty, param_tys),
                name=node.name
            )
            self.functions[node.name] = func

        entry = func.append_basic_block("entry")
        self.builder = ir.IRBuilder(entry)
        self.builder.position_at_end(entry)

        # Initialize concurrent tri-color GC in main function
        if node.name == 'main' and not self.no_gc:
            gc_init_fn = self.functions.get('gc_init')
            if gc_init_fn:
                self.builder.call(gc_init_fn, [])
            gc_start_fn = self.functions.get('gc_start_thread')
            if gc_start_fn:
                self.builder.call(gc_start_fn, [])

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
        for llvm_arg, (name, ptype) in zip(func.args, node.params.items()):
            ptr = self.builder.alloca(llvm_arg.type, name=name)
            self.builder.store(llvm_arg, ptr)
            self.locals[name] = ptr
            self.local_types[name] = ptype

        for stmt in node.body:
            if not self._block_terminated():
                self.emit(stmt)

        if not self._block_terminated():
            # Shutdown GC before main returns
            if node.name == 'main' and not self.no_gc:
                gc_shutdown_fn = self.functions.get('gc_shutdown')
                if gc_shutdown_fn:
                    self.builder.call(gc_shutdown_fn, [])
            if isinstance(ret_ty, ir.VoidType):
                self.builder.ret_void()
            elif isinstance(ret_ty, ir.PointerType):
                self.builder.ret(ir.Constant(ret_ty, None))
            else:
                self.builder.ret(ir.Constant(ret_ty, 0))

        self.locals = old_locals
        self.local_types = old_local_types
        self.ssa_values = old_ssa
        self.ssa_types = old_ssa_types
        self.scope_stack = old_scope_stack
        return None

    @register_emitter(Return)
    def emit_return(self, node: Return):
        if self._block_terminated():
            return None
        # Shutdown GC before main returns
        fn_name = self.builder.function.name
        if fn_name == 'main' and not self.no_gc:
            gc_shutdown_fn = self.functions.get('gc_shutdown')
            if gc_shutdown_fn:
                self.builder.call(gc_shutdown_fn, [])
        if node.value is not None:
            value = self.emit(node.value)
            ret_ty = self.builder.function.ftype.return_type
            if value.type != ret_ty:
                if isinstance(value.type, ir.IntType) and isinstance(ret_ty, ir.IntType):
                    if value.type.width < ret_ty.width:
                        value = self.builder.zext(value, ret_ty)
                    elif value.type.width > ret_ty.width:
                        value = self.builder.trunc(value, ret_ty)
                elif isinstance(value.type, ir.PointerType) and isinstance(ret_ty, ir.IntType):
                    value = self.builder.ptrtoint(value, ret_ty)
                elif isinstance(value.type, ir.IntType) and isinstance(ret_ty, ir.PointerType):
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
        return None

    @staticmethod
    def _switchable_if(node):
        cases = []
        var_name = None

        def extract(n):
            nonlocal var_name
            if not isinstance(n, If):
                return False
            if not isinstance(n.cond, BinOp) or n.cond.op != TokenType.EQ_EQ:
                return False
            if not isinstance(n.cond.left, Variable) or not isinstance(n.cond.right, Number):
                return False
            if var_name is None:
                var_name = n.cond.left.name
            elif n.cond.left.name != var_name:
                return False
            cases.append((n.cond.right, n.body))
            if isinstance(n.orelse, list) and len(n.orelse) == 1 and isinstance(n.orelse[0], If):
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
        return Switch(Variable(var_name), cases)

    @register_emitter(If)
    def emit_if(self, node: If):
        sw = self._switchable_if(node)
        if sw is not None:
            self.emit_switch(sw)
            return
        cond = self.emit(node.cond)
        if cond.type != ir.IntType(1):
            cond = self._is_true(cond)
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
        if isinstance(left.type, ir.DoubleType) and not isinstance(right.type, ir.DoubleType):
            right = self.builder.sitofp(right, ir.DoubleType())
            return left, right
        if isinstance(right.type, ir.DoubleType) and not isinstance(left.type, ir.DoubleType):
            left = self.builder.sitofp(left, ir.DoubleType())
            return left, right

        if isinstance(left.type, ir.PointerType) and isinstance(right.type, ir.PointerType):
            return self.builder.ptrtoint(left, _i64), self.builder.ptrtoint(right, _i64)

        if isinstance(left.type, ir.PointerType) and isinstance(right.type, (ir.FloatType, ir.DoubleType)):
            return self.builder.ptrtoint(left, _i64), self.builder.fptosi(right, _i64)
        if isinstance(right.type, ir.PointerType) and isinstance(left.type, (ir.FloatType, ir.DoubleType)):
            return self.builder.fptosi(left, _i64), self.builder.ptrtoint(right, _i64)

        if isinstance(left.type, ir.PointerType) and not (isinstance(left.type.pointee, ir.IntType) and left.type.pointee.width == 8) and isinstance(right.type, ir.IntType):  # type: ignore[attr-defined]
            left = self.builder.ptrtoint(left, _i64)
            right = self._promote_int(right, _i64)
            return left, right
        if isinstance(right.type, ir.PointerType) and not (isinstance(right.type.pointee, ir.IntType) and right.type.pointee.width == 8) and isinstance(left.type, ir.IntType):  # type: ignore[attr-defined]
            right = self.builder.ptrtoint(right, _i64)
            left = self._promote_int(left, _i64)
            return left, right

        if isinstance(left.type, ir.IntType) and isinstance(right.type, ir.IntType):
            max_width = max(left.type.width, right.type.width)
            left = self._promote_int(left, ir.IntType(max_width))
            right = self._promote_int(right, ir.IntType(max_width))
        return left, right

    def _bitwise_promote(self, left, right):
        if isinstance(left.type, (ir.FloatType, ir.DoubleType)) or isinstance(right.type, (ir.FloatType, ir.DoubleType)):
            src_ty = left.type if isinstance(left.type, (ir.FloatType, ir.DoubleType)) else right.type
            int_ty = ir.IntType(32) if isinstance(src_ty, ir.FloatType) else ir.IntType(64)
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
        elif isinstance(val.type, (ir.FloatType, ir.DoubleType)) and isinstance(target_ty, ir.IntType):
            return self.builder.fptosi(val, target_ty)
        return val

    def _is_true(self, val):
        if val.type == ir.IntType(1):
            return val
        if isinstance(val.type, ir.PointerType):
            return self.builder.icmp_unsigned('!=', val, ir.Constant(val.type, None))
        if isinstance(val.type, (ir.FloatType, ir.DoubleType)):
            return self.builder.fcmp_unordered('!=', val, ir.Constant(val.type, 0.0))
        return self.builder.icmp_signed('!=', val, ir.Constant(val.type, 0))

    def _block_terminated(self) -> bool:
        block = self.builder.block
        if block is None:
            return True
        return block.is_terminated

    @register_emitter(BinOp)
    def emit_binop(self, node):
        if node.op == TokenType.PLUS and self._is_string_concat(node):
            return self._emit_string_concat(node)

        match node.op:
            case TokenType.AND:
                lhs = self.emit(node.left)
                lhs_true = self._is_true(lhs)
                entry_bb = self.builder.block
                rhs_bb = self.builder.append_basic_block("and.rhs")
                end_bb = self.builder.append_basic_block("and.end")
                self.builder.cbranch(lhs_true, rhs_bb, end_bb)
                self.builder.position_at_end(rhs_bb)
                rhs = self.emit(node.right)
                rhs_true = self._is_true(rhs)
                actual_rhs_bb = self.builder.block
                self.builder.branch(end_bb)
                self.builder.position_at_end(end_bb)
                phi = self.builder.phi(_i1)
                phi.add_incoming(ir.Constant(_i1, 0), entry_bb)
                phi.add_incoming(rhs_true, actual_rhs_bb)
                return phi

            case TokenType.OR:
                lhs = self.emit(node.left)
                lhs_true = self._is_true(lhs)
                entry_bb = self.builder.block
                rhs_bb = self.builder.append_basic_block("or.rhs")
                end_bb = self.builder.append_basic_block("or.end")
                self.builder.cbranch(lhs_true, end_bb, rhs_bb)
                self.builder.position_at_end(rhs_bb)
                rhs = self.emit(node.right)
                rhs_true = self._is_true(rhs)
                actual_rhs_bb = self.builder.block
                self.builder.branch(end_bb)
                self.builder.position_at_end(end_bb)
                phi = self.builder.phi(_i1)
                phi.add_incoming(ir.Constant(_i1, 1), entry_bb)
                phi.add_incoming(rhs_true, actual_rhs_bb)
                return phi

        left = self.emit(node.left)
        right = self.emit(node.right)

        # Handle big arithmetic before _promote (which would corrupt i8* big values)
        if self._is_big(node.left) or self._is_big(node.right):
            if not isinstance(left.type, ir.PointerType):
                left = self._promote_to_big(left)
            if not isinstance(right.type, ir.PointerType):
                right = self._promote_to_big(right)
            match node.op:
                case TokenType.PLUS:
                    return self.builder.call(self.functions['bigint_add'], [left, right])
                case TokenType.MINUS:
                    return self.builder.call(self.functions['bigint_sub'], [left, right])
                case TokenType.STAR:
                    return self.builder.call(self.functions['bigint_mul'], [left, right])
                case TokenType.SLASH | TokenType.SLASH_SLASH:
                    return self.builder.call(self.functions['bigint_div'], [left, right])
                case TokenType.PERCENT:
                    return self.builder.call(self.functions['bigint_mod'], [left, right])
                case TokenType.EQ_EQ:
                    cmp = self.builder.call(self.functions['bigint_cmp'], [left, right])
                    return self.builder.icmp_signed('==', cmp, ir.Constant(_i32, 0))
                case TokenType.NOT_EQ:
                    cmp = self.builder.call(self.functions['bigint_cmp'], [left, right])
                    return self.builder.icmp_signed('!=', cmp, ir.Constant(_i32, 0))
                case TokenType.LESS:
                    cmp = self.builder.call(self.functions['bigint_cmp'], [left, right])
                    return self.builder.icmp_signed('<', cmp, ir.Constant(_i32, 0))
                case TokenType.GREATER:
                    cmp = self.builder.call(self.functions['bigint_cmp'], [left, right])
                    return self.builder.icmp_signed('>', cmp, ir.Constant(_i32, 0))
                case TokenType.LESS_EQ:
                    cmp = self.builder.call(self.functions['bigint_cmp'], [left, right])
                    return self.builder.icmp_signed('<=', cmp, ir.Constant(_i32, 0))
                case TokenType.GREATER_EQ:
                    cmp = self.builder.call(self.functions['bigint_cmp'], [left, right])
                    return self.builder.icmp_signed('>=', cmp, ir.Constant(_i32, 0))

        # String comparison via strcmp (content, not pointer comparison)
        left_str = getattr(node.left, 'inferred_type', None) == 'str'
        right_str = getattr(node.right, 'inferred_type', None) == 'str'
        if left_str and right_str:
            cmp = self.builder.call(self._strcmp_fn, [left, right])
            zero = ir.Constant(_i32, 0)
            match node.op:
                case TokenType.EQ_EQ:
                    return self.builder.icmp_signed('==', cmp, zero)
                case TokenType.NOT_EQ:
                    return self.builder.icmp_signed('!=', cmp, zero)
                case TokenType.LESS:
                    return self.builder.icmp_signed('<', cmp, zero)
                case TokenType.GREATER:
                    return self.builder.icmp_signed('>', cmp, zero)
                case TokenType.LESS_EQ:
                    return self.builder.icmp_signed('<=', cmp, zero)
                case TokenType.GREATER_EQ:
                    return self.builder.icmp_signed('>=', cmp, zero)

        left, right = self._promote(left, right)
        is_float = isinstance(left.type, ir.DoubleType) or isinstance(right.type, ir.DoubleType)
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
                    return self.builder.fdiv(left, right)
                case TokenType.PERCENT:
                    return self.builder.frem(left, right)
                case TokenType.GREATER:
                    return self.builder.fcmp_ordered('>', left, right)
                case TokenType.LESS:
                    return self.builder.fcmp_ordered('<', left, right)
                case TokenType.GREATER_EQ:
                    return self.builder.fcmp_ordered('>=', left, right)
                case TokenType.LESS_EQ:
                    return self.builder.fcmp_ordered('<=', left, right)
                case TokenType.EQ_EQ:
                    return self.builder.fcmp_ordered('==', left, right)
                case TokenType.NOT_EQ:
                    return self.builder.fcmp_ordered('!=', left, right)

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
                return self._emit_int_divmod(left, right, is_rem=False)
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
                return self.builder.icmp_signed('>', left, right)
            case TokenType.LESS:
                left, right = self._normalize_ptr_cmp(left, right)
                return self.builder.icmp_signed('<', left, right)
            case TokenType.GREATER_EQ:
                left, right = self._normalize_ptr_cmp(left, right)
                return self.builder.icmp_signed('>=', left, right)
            case TokenType.LESS_EQ:
                left, right = self._normalize_ptr_cmp(left, right)
                return self.builder.icmp_signed('<=', left, right)
            case TokenType.EQ_EQ:
                left, right = self._normalize_ptr_cmp(left, right)
                return self.builder.icmp_signed('==', left, right)
            case TokenType.NOT_EQ:
                left, right = self._normalize_ptr_cmp(left, right)
                return self.builder.icmp_signed('!=', left, right)
            case TokenType.POW:
                return self._emit_pow(left, right)

    def _emit_pow(self, left, right):
        pow_func = self.functions.get("pow")
        if pow_func is None:
            pow_ty = ir.FunctionType(ir.DoubleType(), [ir.DoubleType(), ir.DoubleType()])
            pow_func = ir.Function(self.module, pow_ty, name="pow")
            self.functions["pow"] = pow_func
        was_int = isinstance(left.type, ir.IntType) and isinstance(right.type, ir.IntType)
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
        if isinstance(left.type, ir.PointerType) and isinstance(right.type, ir.PointerType):
            return self.builder.ptrtoint(left, _i64), self.builder.ptrtoint(right, _i64)
        if isinstance(left.type, ir.PointerType) and isinstance(right.type, (ir.FloatType, ir.DoubleType)):
            return self.builder.ptrtoint(left, _i64), self.builder.fptosi(right, _i64)
        if isinstance(right.type, ir.PointerType) and isinstance(left.type, (ir.FloatType, ir.DoubleType)):
            return self.builder.fptosi(left, _i64), self.builder.ptrtoint(right, _i64)
        return left, right

    def _is_string_concat(self, node):
        left_is_str = getattr(node.left, 'inferred_type', None) == 'str'
        right_is_str = getattr(node.right, 'inferred_type', None) == 'str'
        if left_is_str or right_is_str:
            return True
        if isinstance(node.left, Variable) and self.local_types.get(node.left.name) == 'str':
            return True
        if isinstance(node.right, Variable) and self.local_types.get(node.right.name) == 'str':
            return True
        return False

    def _emit_string_concat(self, node):
        left = self.emit(node.left)
        right = self.emit(node.right)
        return self._concat_strings(left, right)

    def _get_strlen_fn(self):
        fn = self._strlen_fn
        if fn is not None:
            return fn
        for f in self.module.functions:
            if f.name == 'strlen':
                self._strlen_fn = f
                return f
        fnty = ir.FunctionType(_i32, [_i8ptr])
        fn = ir.Function(self.module, fnty, 'strlen')
        self._strlen_fn = fn
        return fn

    def _get_memcpy_fn(self):
        fn = self._memcpy_fn
        if fn is not None:
            return fn
        for f in self.module.functions:
            if f.name == 'memcpy':
                self._memcpy_fn = f
                return f
        fnty = ir.FunctionType(_i8ptr, [_i8ptr, _i8ptr, _i32])
        fn = ir.Function(self.module, fnty, 'memcpy')
        self._memcpy_fn = fn
        return fn

    @register_emitter(UnaryOp)
    def emit_unaryop(self, node: UnaryOp):
        match node.op:
            case TokenType.PLUS:
                return self.emit(node.operand)
            case TokenType.MINUS:
                value = self.emit(node.operand)
                if self._is_big(node.operand) and isinstance(value.type, ir.PointerType):
                    return self.builder.call(self.functions['bigint_neg'], [value])
                if isinstance(value.type, ir.PointerType):
                    int_ty = ir.IntType(64)
                    as_int = self.builder.ptrtoint(value, int_ty)
                    neg = self.builder.sub(ir.Constant(int_ty, 0), as_int)
                    return self.builder.inttoptr(neg, value.type)
                zero = ir.Constant(value.type, 0.0 if isinstance(value.type, ir.DoubleType) else 0)
                if isinstance(value.type, ir.DoubleType):
                    return self.builder.fsub(zero, value)
                return self.builder.sub(zero, value)
            case TokenType.MINUS_MINUS:
                value = self.emit(node.operand)
                if isinstance(value.type, ir.PointerType):
                    neg_one = ir.Constant(_i32, -1)
                    return self.builder.gep(value, [neg_one], inbounds=True)
                one = ir.Constant(value.type, 1.0 if isinstance(value.type, ir.DoubleType) else 1)
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
                    int_ty = ir.IntType(32) if isinstance(value.type, ir.FloatType) else ir.IntType(64)
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
                    return self.builder.icmp_unsigned('==', value, zero)
                if isinstance(value.type, (ir.FloatType, ir.DoubleType)):
                    zero = ir.Constant(value.type, 0.0)
                    return self.builder.fcmp_unordered('==', value, zero)
                zero = ir.Constant(value.type, 0)
                return self.builder.icmp_unsigned('==', value, zero)
        return None

    #Added constant check code
    #Is it good man?
    @register_emitter(Variable)
    def emit_variable(self, node):
        const = self.const_vars.get(node.name)
        if const is not None:
            # Compile-time constant
            if isinstance(const, ir.Constant):
                return const
            # Runtime constant stored in immutable global
            return self.builder.load(const, node.name)

        # Constants resolved during semantic analysis (enum members, imported C constants)
        if getattr(node, 'const_value', None) is not None:
            val = node.const_value
            # Use i32 for small values, i64 for large ones
            if -2**31 <= val < 2**31:
                return ir.Constant(ir.IntType(32), val)
            return ir.Constant(ir.IntType(64), val)

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
        return (isinstance(value.type, ir.IntType) and value.type.width == 8
                and isinstance(pointee, ir.PointerType)
                and getattr(pointee.pointee, 'width', None) == 8)

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
        if isinstance(pointee, ir.PointerType) and isinstance(value.type, ir.PointerType):
            return self.builder.bitcast(value, pointee)
        if isinstance(pointee, ir.IntType) and isinstance(value.type, ir.PointerType):
            if pointee.width == 8 and isinstance(value.type.pointee, ir.IntType) and value.type.pointee.width == 8:  # type: ignore[attr-defined]
                loaded = self.builder.load(value)
                if loaded.type != pointee:
                    loaded = self._trunc_or_ext(loaded, pointee)
                return loaded
            i64_ty = ir.IntType(64)
            ptr_val = self.builder.ptrtoint(value, i64_ty)
            if pointee.width < 64:
                return self.builder.trunc(ptr_val, pointee)
            return ptr_val
        if isinstance(pointee, (ir.FloatType, ir.DoubleType)) and isinstance(value.type, ir.IntType):
            return self.builder.sitofp(value, pointee)
        if isinstance(pointee, (ir.FloatType, ir.DoubleType)) and isinstance(value.type, (ir.FloatType, ir.DoubleType)):
            src_w = 64 if isinstance(value.type, ir.DoubleType) else 32
            dst_w = 64 if isinstance(pointee, ir.DoubleType) else 32
            return self.builder.fpext(value, pointee) if src_w < dst_w else self.builder.fptrunc(value, pointee)
        if isinstance(pointee, ir.IntType) and isinstance(value.type, (ir.FloatType, ir.DoubleType)):
            return self.builder.fptosi(value, pointee)
        if isinstance(pointee, (ir.FloatType, ir.DoubleType)) and isinstance(value.type, ir.PointerType):
            i64_ty = ir.IntType(64)
            ptr_val = self.builder.ptrtoint(value, i64_ty)
            return self.builder.sitofp(ptr_val, pointee)
        if isinstance(pointee, ir.PointerType) and isinstance(value.type, (ir.FloatType, ir.DoubleType)):
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
        if isinstance(node.target, Variable):
            name = node.target.name

            # Constant assignment check :)
            if name in self.const_vars:
                raise Exception(f"Cannot assign to constant '{name}' at L{node._token.line}:{node._token.column}")

            ptr = self.locals.get(name)
            if ptr is not None:
                value = self.emit(node.value)
                pointee = self._pointee_type(ptr)
                if pointee and value.type != pointee:
                    if self.local_types.get(name) == 'big' and not self._is_big(node.value):
                        value = self._promote_to_big(value)
                    else:
                        value = self._coerce_store(value, pointee)
                var_type = self.local_types.get(name)
                if var_type in ('int64', 'uint64'):
                    value = self._extend_to_i64(value)
                self.builder.store(value, ptr)
                self.ssa_types[name] = var_type
                return None
            value = self.emit(node.value)
            ssa = self.ssa_values.pop(name, None)
            ptr = self._alloca(value.type, name)
            self._declare_local(name, ptr, str(value.type))
            self.builder.store(value, ptr)
            return None
        if isinstance(node.target, str):
            name = node.target

            # Constant assignment check :) (AGAIN!)
            if name in self.const_vars:
                raise Exception(f"Cannot assign to constant '{name}' at L{node._token.line}:{node._token.column}")

            ptr = self.locals.get(name)
            if ptr is not None:
                value = self.emit(node.value)
                pointee = self._pointee_type(ptr)
                if pointee and value.type != pointee:
                    if self.local_types.get(name) == 'big' and not self._is_big(node.value):
                        value = self._promote_to_big(value)
                    else:
                        value = self._coerce_store(value, pointee)
                var_type = self.local_types.get(name)
                if var_type in ('int64', 'uint64'):
                    value = self._extend_to_i64(value)
                self.builder.store(value, ptr)
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
            elif hasattr(node.callee.obj, '_inferred_type'):
                obj_type_name = node.callee.obj._inferred_type
            if obj_type_name:
                # Strip pointer
                if obj_type_name.endswith('*'):
                    obj_type_name = obj_type_name[:-1]
                # Resolve generic types
                resolved = self._resolve_generic_type(obj_type_name)
                if resolved is not None:
                    obj_type_name = resolved
                method_name = f'{obj_type_name}.{callee_name}'
                func = self.functions.get(method_name)
                if func:
                    args = [obj_val]
                    for arg in node.args:
                        args.append(self.emit(arg))
                    return self.builder.call(func, args)

        # Handle known macro functions by inlining
        if node.callee.name == 'CGEventMaskBit':
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
            raise Exception(f"Undefined function '{node.callee.name}' at L{node._token.line}:{node._token.column}")
        args = []
        for i, arg in enumerate(node.args):
            val = self.emit(arg)
            if i < len(func.function_type.args):
                expected = func.function_type.args[i]
                if isinstance(val, ir.Constant) and val.constant == 0 and isinstance(val.type, ir.IntType):
                    if isinstance(expected, ir.PointerType):
                        val = ir.Constant(expected, None)
                if isinstance(val.type, ir.IntType) and isinstance(expected, ir.PointerType):
                    val = self.builder.inttoptr(val, expected)
                if isinstance(val.type, ir.PointerType) and isinstance(expected, ir.IntType):
                    val = self.builder.ptrtoint(val, expected)
                if isinstance(val.type, ir.PointerType) and isinstance(expected, ir.PointerType) and val.type != expected:
                    val = self.builder.bitcast(val, expected)
                if isinstance(val.type, ir.IntType) and isinstance(expected, ir.DoubleType):
                    val = self.builder.sitofp(val, expected)
                if isinstance(val.type, ir.DoubleType) and isinstance(expected, ir.IntType):
                    val = self.builder.fptosi(val, expected)
            args.append(val)
        return self.builder.call(func, args)

    @register_emitter(Print)
    def emit_print(self, node):
        if self.no_userspace:
            # In no-userspace mode, print statements become no-ops
            # Still emit the value for side effects, but don't call print function
            self.emit(node.value)
            return None
        
        value = self.emit(node.value)
        if self._is_big(node.value):
            return self.builder.call(self.functions["bigint_print"], [value])
        if isinstance(value.type, ir.DoubleType):
            return self.builder.call(self.functions["print_double"], [value])
        if (isinstance(value.type, ir.PointerType)
                and isinstance(value.type.pointee, ir.IntType)  # type: ignore[attr-defined]
                and value.type.pointee.width == 8):  # type: ignore[attr-defined]
            return self.builder.call(self.functions["print_str"], [value])
        # Handle 64-bit integers
        if isinstance(value.type, ir.IntType) and value.type.width == 64:
            is_uint64 = False
            if isinstance(node.value, Variable):
                var_type = self.local_types.get(node.value.name)
                if var_type == 'uint64':
                    is_uint64 = True
            elif hasattr(node.value, 'inferred_type') and node.value.inferred_type == 'uint64':
                is_uint64 = True
            elif isinstance(node.value, BinOp):
                left_type = None
                right_type = None
                if isinstance(node.value.left, Variable):
                    left_type = self.local_types.get(node.value.left.name)
                elif hasattr(node.value.left, 'inferred_type'):
                    left_type = node.value.left.inferred_type
                if isinstance(node.value.right, Variable):
                    right_type = self.local_types.get(node.value.right.name)
                elif hasattr(node.value.right, 'inferred_type'):
                    right_type = node.value.right.inferred_type
                if left_type == 'uint64' or right_type == 'uint64':
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
            return self.builder.call(self.functions["print_hex"], [self.builder.ptrtoint(value, _i64)])
        return self.builder.call(self.functions["print_int64"], [self.builder.ptrtoint(value, _i64)])

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
            return self.builder.call(self.functions["bigint_from_int"], [ir.Constant(_i64, 0)])
        func = self.functions["bigint_input"]
        return self.builder.call(func, [])

    @register_emitter(Signed67)
    def emit_signed67(self, node):
        key = b'cpyte-easter-egg-2024'
        sig = hmac.new(key, b'67', hashlib.sha256).hexdigest()
        return self._string_const(sig)

    @register_emitter(While)
    def emit_while(self, node):
        cond_bb = self.builder.append_basic_block("while.cond")
        body_bb = self.builder.append_basic_block("while.body")
        end_bb = self.builder.append_basic_block("while.end")

        self.builder.branch(cond_bb)
        self.builder.position_at_end(cond_bb)

        cond = self.emit(node.cond)
        if cond.type != ir.IntType(1):
            cond = self._is_true(cond)
        self.builder.cbranch(cond, body_bb, end_bb)
        self.builder.position_at_end(body_bb)

        self.loop_stack.append((cond_bb, end_bb))
        self._push_scope()
        for stmt in node.body:
            if not self._block_terminated():
                self.emit(stmt)
        self._pop_scope()
        self.loop_stack.pop()

        if not self._block_terminated():
            self.builder.branch(cond_bb)

        self.builder.position_at_end(end_bb)

    @register_emitter(Break)
    def emit_break(self, node):
        if not self.loop_stack:
            return None
        _, end_bb = self.loop_stack[-1]
        self.builder.branch(end_bb)

    @register_emitter(Continue)
    def emit_continue(self, node):
        if not self.loop_stack:
            return None
        cond_bb, _ = self.loop_stack[-1]
        self.builder.branch(cond_bb)

    def emit_for(self, node):
        var_name = node['var']
        iterable = node['iter']
        body = node['body']

        iter_ptr = self.emit(iterable)

        char_ptr_ty = ir.PointerType(_i8)
        if iter_ptr.type != char_ptr_ty:
            iter_ptr = self.builder.bitcast(iter_ptr, char_ptr_ty)

        len_fn = self._get_strlen_fn()
        length = self.builder.call(len_fn, [iter_ptr])

        idx_ptr = self._alloca(_i32, name=f"{var_name}.idx")
        self.builder.store(ir.Constant(_i32, 0), idx_ptr)

        self._push_scope()
        var_ptr = self._alloca(_i8, name=var_name)
        self._declare_local(var_name, var_ptr, 'char')

        cond_bb = self.builder.append_basic_block(f"for.{var_name}.cond")
        body_bb = self.builder.append_basic_block(f"for.{var_name}.body")
        inc_bb = self.builder.append_basic_block(f"for.{var_name}.inc")
        end_bb = self.builder.append_basic_block(f"for.{var_name}.end")

        self.builder.branch(cond_bb)
        self.builder.position_at_end(cond_bb)

        idx = self.builder.load(idx_ptr)
        cmp = self.builder.icmp_signed('<', idx, length)
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

    @register_emitter(Number)
    def emit_number(self, node):
        if getattr(node, 'inferred_type', '') == 'big':
            s = node.value
            if s.startswith('0x') or s.startswith('0X'):
                s = str(int(s, 16))
            key = s + '\0'
            g = self.biglit_pool.get(key)
            if g is None:
                arr_ty = ir.ArrayType(_i8, len(key))
                g = ir.GlobalVariable(self.module, arr_ty, f'.biglit.{len(self.biglit_pool)}')
                g.initializer = ir.Constant(arr_ty, bytearray(key.encode()))  # type: ignore[attr-defined]
                g.global_constant = True
                self.biglit_pool[key] = g
            ptr = self.builder.bitcast(g, _i8ptr)
            return self.builder.call(self.functions['bigint_from_str'], [ptr])

        if '.' in node.value:
            return ir.Constant(ir.DoubleType(), float(node.value))
        
        # Handle hexadecimal literals
        if node.value.startswith('0x') or node.value.startswith('0X'):
            value = int(node.value, 16)
            # Use 64-bit for large hex values
            if value > 2**31 - 1 or value < -2**31:
                return ir.Constant(ir.IntType(64), value)
            return ir.Constant(ir.IntType(32), value)
        
        value = int(node.value)
        # Use 64-bit for large decimal values
        if value > 2**31 - 1 or value < -2**31:
            return ir.Constant(ir.IntType(64), value)
        return ir.Constant(ir.IntType(32), value)

    @register_emitter(String)
    def emit_string(self, node):
        return self._string_const(node.value)

    def _string_const(self, val: str):
        val = val + '\0'
        if val not in self.string_pool:
            arr_ty = ir.ArrayType(ir.IntType(8), len(val))
            init = ir.Constant(arr_ty, bytearray(val.encode('utf-8')))
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

    def _alloca(self, ty, name=''):
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
        ty = self.llvm_type(node.var_type)
        if node.is_const:
            if node.init:
                value = self.emit(node.init)
                if node.var_type == 'big' and not self._is_big(node.init):
                    value = self._promote_to_big(value)
                elif node.var_type in ('int64', 'uint64'):
                    value = self._extend_to_i64(value)
                if isinstance(value, ir.Constant):
                    self._declare_const(node.name, value)
                else:
                    ptr = self._alloca(ty, name=node.name)
                    self.builder.store(value, ptr)
                    self._declare_const(node.name, ptr)
            else:
                self._declare_const(node.name, ir.Constant(ty, 0))
            return None
        ptr = self._alloca(ty, name=node.name)
        self._declare_local(node.name, ptr, node.var_type)
        if node.init:
            value = self.emit(node.init)
            if node.var_type == 'big' and not self._is_big(node.init):
                value = self._promote_to_big(value)
            elif self._is_big(node.init) and node.var_type != 'big':
                pass
            elif node.var_type in ('int64', 'uint64'):
                value = self._extend_to_i64(value)
            if value.type != ty:
                if isinstance(value.type, ir.IntType) and isinstance(ty, ir.PointerType):
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
                elif isinstance(value.type, ir.PointerType) and isinstance(ty, ir.IntType):
                    value = self.builder.load(value)
                    if value.type != ty:
                        value = self.builder.trunc(value, ty)
                elif isinstance(value.type, ir.PointerType) and isinstance(ty, ir.PointerType):
                    value = self.builder.bitcast(value, ty)
                elif isinstance(value.type, ir.IntType) and isinstance(ty, (ir.FloatType, ir.DoubleType)):
                    value = self.builder.sitofp(value, ty)
                elif isinstance(value.type, (ir.FloatType, ir.DoubleType)) and isinstance(ty, ir.PointerType):
                    i64_ty = ir.IntType(64)
                    int_val = self.builder.fptosi(value, i64_ty)
                    value = self.builder.inttoptr(int_val, ty)
                elif isinstance(value.type, (ir.FloatType, ir.DoubleType)) and isinstance(ty, ir.IntType):
                    value = self.builder.fptosi(value, ty)
            self.builder.store(value, ptr)
        elif isinstance(ty, ir.PointerType):
            self.builder.store(ir.Constant(ty, None), ptr)
        elif isinstance(ty, (ir.IntType, ir.FloatType, ir.DoubleType)):
            self.builder.store(ir.Constant(ty, 0), ptr)

    @register_emitter(Import)
    def emit_import(self, node):
        var_names = getattr(node, 'var_names', set()) or set()
        for fname, (ret_type, params, vararg) in node.symbols:
            if fname in self.functions or fname in self.global_vars:
                continue
            if fname in var_names:
                # Variable declaration (e.g., CF_EXPORT const ...)
                var_ty = self.llvm_type(ret_type)
                if isinstance(var_ty, ir.VoidType):
                    continue
                gv = ir.GlobalVariable(self.module, var_ty, name=fname)
                gv.linkage = 'extern_weak'
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
        if getattr(node, 'prebuilt_ll_files', None):
            self.import_src_files.extend(node.prebuilt_ll_files)
