import hmac
import hashlib

from llvmlite import ir

from .lexar import Token, TokenType
from .astparse import *


_i8 = ir.IntType(8)
_i32 = ir.IntType(32)
_i64 = ir.IntType(64)
_i1 = ir.IntType(1)
_double = ir.DoubleType()
_void = ir.VoidType()
_i8ptr = ir.PointerType(_i8)


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
        base_name = t.split('<')[0]
        if base_name in self.structs:
            return self.structs[base_name]
        return ir.IntType(32)

    def _base_type_name(self, t: str) -> str:
        while t.endswith('[]'):
            t = t[:-2]
        while t.endswith('*') or t.endswith('&'):
            t = t[:-1]
        idx = t.find('<')
        if idx != -1:
            t = t[:idx]
        return t

    def _is_ir_constant_zero(self, val):
        if isinstance(val, ir.Constant) and val.constant == 0:
            return True
        if isinstance(val, ir.Constant) and isinstance(val.type, ir.PointerType) and val.constant is None:
            return True
        return False

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

    def __init__(self):
        self.module = ir.Module("main")
        self.builder = None
        self.functions = {}
        self.global_vars = {}
        self.locals = {}
        self.local_types = {}
        self.string_id = 0
        self.string_pool = {}
        self.ssa_values = {}
        self.ssa_types = {}  # Track types of SSA values
        self.structs = {}
        self.struct_fields = {}
        self.import_src_files = []
        self.loop_stack = []
        self._malloc_fn = None
        self._strlen_fn = None
        self._memcpy_fn = None
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
        print_s_ty = ir.FunctionType(ir.VoidType(), [ir.PointerType(ir.IntType(8))])
        print_s_fn = ir.Function(self.module, print_s_ty, "print_str")
        self.functions["print_str"] = print_s_fn
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
            ('bigint_add', _i8ptr, [_i8ptr, _i8ptr]),
            ('bigint_sub', _i8ptr, [_i8ptr, _i8ptr]),
            ('bigint_mul', _i8ptr, [_i8ptr, _i8ptr]),
            ('bigint_div', _i8ptr, [_i8ptr, _i8ptr]),
            ('bigint_mod', _i8ptr, [_i8ptr, _i8ptr]),
            ('bigint_neg', _i8ptr, [_i8ptr]),
            ('bigint_cmp', _i64, [_i8ptr, _i8ptr]),
            ('bigint_print', _void, [_i8ptr]),
        ]
        for name, ret, args in bignum_fns:
            fn = ir.Function(self.module, ir.FunctionType(ret, args), name=name)
            self.functions[name] = fn

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
                    for stmt in body:
                        self.emit(stmt)
                    if not self.builder.block.is_terminated:
                        self.builder.branch(end_blk)
                    self.builder.position_at_start(nxt_blk)
                else:
                    self.builder.branch(default_blk)
                    self.builder.position_at_start(default_blk)
                    for stmt in body:
                        self.emit(stmt)
                    if not self.builder.block.is_terminated:
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
            for stmt in body:
                self.emit(stmt)
            if not self.builder.block.is_terminated:
                self.builder.branch(end_blk)

        self.builder.position_at_start(end_blk)

    def emit_program(self, ast):
        structs = []
        imports = []
        funcdefs = []
        toplevel = []

        def _collect_nodes(nodes):
            for node in nodes:
                if isinstance(node, StructDef):
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

        for node in structs:
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
            for node in toplevel:
                self.emit(node)
            self.builder.ret(ir.Constant(ir.IntType(32), 0))

        return self.module, self.import_src_files

    def emit(self, node: list[Token]):
        if isinstance(node, FuncDef):
            return self.funcdo(node)
        if isinstance(node, Return):
            return self.emit_return(node)
        if isinstance(node, If):
            return self.emit_if(node)
        if isinstance(node, BinOp):
            return self.emit_binop(node)
        if isinstance(node, UnaryOp):
            return self.emit_unary(node)
        if isinstance(node, Variable):
            return self.emit_variable(node)
        if isinstance(node, Assign):
            return self.emit_assign(node)
        if isinstance(node, Call):
            return self.emit_call(node)
        if isinstance(node, While):
            return self.emit_while(node)
        if isinstance(node, Print):
            return self.emit_print(node)
        if isinstance(node, Input):
            return self.emit_input(node)
        if isinstance(node, InputStr):
            return self.emit_input_str(node)
        if isinstance(node, Signed67):
            return self.emit_signed_67(node)
        if isinstance(node, Number):
            return self.emit_number(node)
        if isinstance(node, String):
            return self.emit_string(node)
        if isinstance(node, VarDecl):
            return self.emit_var_decl(node)
        if isinstance(node, ExprStmt):
            return self.emit(node.expr)
        if isinstance(node, Import):
            return self.emit_import(node)
        if isinstance(node, StructDef):
            return self.emit_struct(node)
        if isinstance(node, NewExpr):
            return self.emit_new_expr(node)
        if isinstance(node, Deref):
            return self.emit_deref(node)
        if isinstance(node, AddrOf):
            return self.emit_addr_of(node)
        if isinstance(node, SizeOf):
            return self.emit_sizeof(node)
        if isinstance(node, Index):
            return self.emit_index(node)
        if isinstance(node, Attr):
            return self.emit_attr(node)
        if isinstance(node, Switch):
            return self.emit_switch(node)
        if isinstance(node, Break):
            return self.emit_break(node)
        if isinstance(node, Continue):
            return self.emit_continue(node)
        if isinstance(node, dict) and node.get('type') == 'for':
            return self.emit_for(node)
        return None

    def emit_struct(self, node: StructDef):
        field_tys = []
        for f in node.fields:
            field_tys.append(self.llvm_type(f.type_expr))
        llvm_struct = self.structs.get(node.name)
        if llvm_struct is not None and isinstance(llvm_struct, ir.IdentifiedStructType):
            llvm_struct.set_body(*field_tys)
        else:
            llvm_struct = ir.LiteralStructType(field_tys)
            llvm_struct.name = f"struct.{node.name}"
            self.structs[node.name] = llvm_struct
        self.struct_fields[node.name] = node.fields

    def emit_new_expr(self, node: NewExpr):
        if node.type_expr == 'str':
            return self._string_const('')
        if node.size is not None:
            count = self.emit(node.size)
            if isinstance(count.type, ir.PointerType):
                count = self.builder.ptrtoint(count, _i64)
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
        sz, _ = self._type_abi_info(ty)
        return ir.Constant(ir.IntType(32), sz)

    def emit_deref(self, node: Deref):
        ptr = self.emit(node.operand)
        return self.builder.load(ptr)

    def emit_addr_of(self, node: AddrOf):
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

    def emit_sizeof(self, node: SizeOf):
        ty = self.llvm_type(node.type_expr)
        return self._sizeof_type(ty)

    def emit_index(self, node: Index):
        ptr = self._emit_lvalue(node)
        return self.builder.load(ptr)

    def _emit_lvalue_index(self, node: Index):
        obj = self.emit(node.obj)
        idx = self.emit(node.index)
        if isinstance(idx.type, ir.PointerType):
            idx = self.builder.ptrtoint(idx, _i64)
        return self.builder.gep(obj, [idx], inbounds=True)

    def emit_attr(self, node: Attr):
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
        if isinstance(node, Deref) and isinstance(node.operand, Variable):
            declared = self.local_types.get(node.operand.name, '')
            if declared.endswith('*'):
                base = declared[:-1]
                return self._base_type_name(base)
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
        if struct_name and struct_name in self.struct_fields:
            fields = self.struct_fields[struct_name]
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
            ssa = self.ssa_values.pop(name, None)
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

    def _infer_llvm_type(self, node):
        if isinstance(node, Variable):
            ptr = self.locals.get(node.name)
            if ptr:
                return ptr.type.pointee
            return None
        if isinstance(node, Index):
            obj_t = self._infer_llvm_type(node.obj)
            if isinstance(obj_t, ir.PointerType):
                return obj_t.pointee
            return None
        if isinstance(node, Attr):
            return None
        return None

    def funcdo(self, node: FuncDef):
        ret_ty = self.llvm_type(getattr(node, 'rettype', None) or 'void')
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

        old_locals = self.locals
        old_local_types = self.local_types
        self.locals = {}
        self.local_types = {}
        for llvm_arg, (name, ptype) in zip(func.args, node.params.items()):
            ptr = self.builder.alloca(llvm_arg.type, name=name)
            self.builder.store(llvm_arg, ptr)
            self.locals[name] = ptr
            self.local_types[name] = ptype

        for stmt in node.body:
            if not self.builder.block.is_terminated:
                self.emit(stmt)

        if not self.builder.block.is_terminated:
            if isinstance(ret_ty, ir.VoidType):
                self.builder.ret_void()
            elif isinstance(ret_ty, ir.PointerType):
                self.builder.ret(ir.Constant(ret_ty, None))
            else:
                self.builder.ret(ir.Constant(ret_ty, 0))

        self.locals = old_locals
        self.local_types = old_local_types
        return None

    def emit_return(self, node: Return):
        if self.builder.block.is_terminated:
            return None
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
            self.builder.ret_void()
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
        if len(cases) < 2:
            return None
        return Switch(Variable(var_name), cases)

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
        for stmt in node.body:
            if not self.builder.block.is_terminated:
                self.emit(stmt)
        if not self.builder.block.is_terminated:
            self.builder.branch(end_bb)

        if node.orelse:
            self.builder.position_at_end(else_bb)
            for stmt in node.orelse:
                if not self.builder.block.is_terminated:
                    self.emit(stmt)
            if not self.builder.block.is_terminated:
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

        if isinstance(left.type, ir.PointerType) and not (isinstance(left.type.pointee, ir.IntType) and left.type.pointee.width == 8) and isinstance(right.type, ir.IntType):
            left = self.builder.ptrtoint(left, _i64)
            right = self._promote_int(right, _i64)
            return left, right
        if isinstance(right.type, ir.PointerType) and not (isinstance(right.type.pointee, ir.IntType) and right.type.pointee.width == 8) and isinstance(left.type, ir.IntType):
            right = self.builder.ptrtoint(right, _i64)
            left = self._promote_int(left, _i64)
            return left, right

        if isinstance(left.type, ir.IntType) and isinstance(right.type, ir.IntType):
            max_width = max(left.type.width, right.type.width)
            left = self._promote_int(left, ir.IntType(max_width))
            right = self._promote_int(right, ir.IntType(max_width))
        return left, right

    def _bitwise_promote(self, left, right):
        if isinstance(left.type, (ir.FloatType, ir.DoubleType)):
            int_ty = ir.IntType(32) if isinstance(left.type, ir.FloatType) else ir.IntType(64)
            left = self.builder.bitcast(left, int_ty)
            right = self.builder.bitcast(right, int_ty)
        return left, right

    def _promote_int(self, val, target_ty):
        if val.type == target_ty:
            return val
        if isinstance(val.type, ir.IntType) and isinstance(target_ty, ir.IntType):
            if val.type.width < target_ty.width:
                if val.type.width == 32:
                    return self.builder.sext(val, target_ty)
                return self.builder.zext(val, target_ty)
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
                phi = self.builder.phi(res_ty)
                phi.add_incoming(lhs, entry_bb)
                phi.add_incoming(rhs, actual_rhs_bb)
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
                    return self.builder.icmp_signed('==', cmp, ir.Constant(_i64, 0))
                case TokenType.NOT_EQ:
                    cmp = self.builder.call(self.functions['bigint_cmp'], [left, right])
                    return self.builder.icmp_signed('!=', cmp, ir.Constant(_i64, 0))
                case TokenType.LESS:
                    cmp = self.builder.call(self.functions['bigint_cmp'], [left, right])
                    return self.builder.icmp_signed('<', cmp, ir.Constant(_i64, 0))
                case TokenType.GREATER:
                    cmp = self.builder.call(self.functions['bigint_cmp'], [left, right])
                    return self.builder.icmp_signed('>', cmp, ir.Constant(_i64, 0))
                case TokenType.LESS_EQ:
                    cmp = self.builder.call(self.functions['bigint_cmp'], [left, right])
                    return self.builder.icmp_signed('<=', cmp, ir.Constant(_i64, 0))
                case TokenType.GREATER_EQ:
                    cmp = self.builder.call(self.functions['bigint_cmp'], [left, right])
                    return self.builder.icmp_signed('>=', cmp, ir.Constant(_i64, 0))

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
                if self._is_ir_constant_zero(right):
                    raise ZeroDivisionError('division by zero')
                return self.builder.sdiv(left, right)
            case TokenType.SLASH_SLASH:
                if self._is_ir_constant_zero(right):
                    raise ZeroDivisionError('division by zero')
                return self.builder.sdiv(left, right)
            case TokenType.PERCENT:
                if self._is_ir_constant_zero(right):
                    raise ZeroDivisionError('division by zero')
                return self.builder.srem(left, right)
            case TokenType.SHL:
                left, right = self._bitwise_promote(left, right)
                return self.builder.shl(left, right)
            case TokenType.SHR:
                left, right = self._bitwise_promote(left, right)
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
        if isinstance(node.left, String) or isinstance(node.right, String):
            return True
        if isinstance(node.left, Variable) and self.local_types.get(node.left.name) == 'str':
            return True
        if isinstance(node.right, Variable) and self.local_types.get(node.right.name) == 'str':
            return True
        return False

    def _emit_string_concat(self, node):
        left = self.emit(node.left)
        right = self.emit(node.right)
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

    def emit_unary(self, node: UnaryOp):
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

    def emit_variable(self, node):
        if node.const_value is not None:
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
        # Check if it's a defined function (to pass as function pointer)
        func = self.functions.get(node.name)
        if func is not None:
            # Return a pointer to the function (for use as callback arg)
            ptr_ty = ir.PointerType(ir.IntType(8))
            return self.builder.bitcast(func, ptr_ty)
        raise Exception(f"Undefined variable '{node.name}' at L{node._token.line}:{node._token.column}")

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

    def _coerce_store(self, value, pointee):
        if isinstance(pointee, ir.IntType) and isinstance(value.type, ir.IntType):
            return self._trunc_or_ext(value, pointee)
        if isinstance(pointee, ir.PointerType) and isinstance(value.type, ir.IntType):
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
            if pointee.width == 8 and isinstance(value.type.pointee, ir.IntType) and value.type.pointee.width == 8:
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
            return self.builder.fpext(value, pointee) if value.type.width < pointee.width else self.builder.fptrunc(value, pointee)
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

    def emit_assign(self, node):
        if isinstance(node.target, Variable):
            name = node.target.name
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
            self.locals[name] = ptr
            self.local_types[name] = str(value.type)
            self.builder.store(value, ptr)
            return None
        if isinstance(node.target, str):
            name = node.target
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
            self.locals[name] = ptr
            self.local_types[name] = str(value.type)
            self.builder.store(value, ptr)
            return None

        target_ptr = self._emit_lvalue(node.target)
        value = self.emit(node.value)
        pointee = self._pointee_type(target_ptr)
        if pointee and value.type != pointee:
            value = self._coerce_store(value, pointee)
        self.builder.store(value, target_ptr)
        return None

    def emit_call(self, node):
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
            args.append(val)
        return self.builder.call(func, args)

    def emit_print(self, node):
        value = self.emit(node.value)
        if self._is_big(node.value):
            return self.builder.call(self.functions["bigint_print"], [value])
        if isinstance(value.type, ir.DoubleType):
            return self.builder.call(self.functions["print_double"], [value])
        if (isinstance(value.type, ir.PointerType)
                and isinstance(value.type.pointee, ir.IntType)
                and value.type.pointee.width == 8):
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
        # Handle non-string pointers: convert to int64 and print
        if isinstance(value.type, ir.PointerType):
            value = self.builder.ptrtoint(value, _i64)
            return self.builder.call(self.functions["print_int64"], [value])
        # Handle i32 — call print_int directly
        if isinstance(value.type, ir.IntType):
            return self.builder.call(self.functions["print_int"], [value])
        # Fallback: convert to int64 and print
        if isinstance(value.type, (ir.FloatType, ir.DoubleType)):
            return self.builder.call(self.functions["print_double"], [value])
        if isinstance(value.type, ir.PointerType):
            return self.builder.call(self.functions["print_int64"], [self.builder.ptrtoint(value, _i64)])
        return self.builder.call(self.functions["print_int64"], [self.builder.ptrtoint(value, _i64)])

    def emit_input(self, node):
        func = self.functions["input"]
        return self.builder.call(func, [])

    def emit_input_str(self, node):
        func = self.functions["input_str"]
        return self.builder.call(func, [])

    def emit_signed_67(self, node):
        key = b'cpyte-easter-egg-2024'
        sig = hmac.new(key, b'67', hashlib.sha256).hexdigest()
        return self._string_const(sig)

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
        for stmt in node.body:
            if not self.builder.block.is_terminated:
                self.emit(stmt)
        self.loop_stack.pop()

        if not self.builder.block.is_terminated:
            self.builder.branch(cond_bb)

        self.builder.position_at_end(end_bb)

    def emit_break(self, node):
        if not self.loop_stack:
            return None
        _, end_bb = self.loop_stack[-1]
        self.builder.branch(end_bb)

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

        if var_name in self.locals:
            var_ptr = self.locals[var_name]
        else:
            var_ptr = self._alloca(_i8, name=var_name)

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

        self.locals[var_name] = var_ptr
        self.local_types[var_name] = 'char'

        self.loop_stack.append((inc_bb, end_bb))
        for stmt in body:
            if not self.builder.block.is_terminated:
                self.emit(stmt)
        self.loop_stack.pop()

        if not self.builder.block.is_terminated:
            self.builder.branch(inc_bb)

        self.builder.position_at_end(inc_bb)
        idx_inc = self.builder.load(idx_ptr)
        idx_next = self.builder.add(idx_inc, ir.Constant(_i32, 1))
        self.builder.store(idx_next, idx_ptr)
        self.builder.branch(cond_bb)

        self.builder.position_at_end(end_bb)

    def emit_number(self, node):
        if getattr(node, 'inferred_type', '') == 'big':
            s = node.value + '\0'
            arr_ty = ir.ArrayType(_i8, len(s))
            g = ir.GlobalVariable(self.module, arr_ty, f'.biglit.{id(node)}')
            g.initializer = ir.Constant(arr_ty, bytearray(s.encode()))
            g.global_constant = True
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
            gv.initializer = init
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

    def emit_var_decl(self, node):
        ty = self.llvm_type(node.var_type)
        ptr = self._alloca(ty, name=node.name)
        self.locals[node.name] = ptr
        self.local_types[node.name] = node.var_type
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
