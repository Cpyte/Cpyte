from llvmlite import ir

from lexar import Token, TokenType
from astparse import *


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

    def __init__(self):
        self.module = ir.Module("main")
        self.builder = None
        self.functions = {}
        self.locals = {}
        self.local_types = {}
        self.string_id = 0
        self.string_pool = {}
        self.ssa_values = {}
        self.structs = {}
        self.struct_fields = {}
        self.import_src_files = []
        print_ty = ir.FunctionType(ir.VoidType(), [ir.IntType(32)])
        print_fn = ir.Function(self.module, print_ty, "print_int")
        self.functions["print_int"] = print_fn
        print_i64_ty = ir.FunctionType(ir.VoidType(), [ir.IntType(64)])
        print_i64_fn = ir.Function(self.module, print_i64_ty, "print_int64")
        self.functions["print_int64"] = print_i64_fn
        print_f_ty = ir.FunctionType(ir.VoidType(), [ir.DoubleType()])
        print_f_fn = ir.Function(self.module, print_f_ty, "print_double")
        self.functions["print_double"] = print_f_fn
        print_s_ty = ir.FunctionType(ir.VoidType(), [ir.PointerType(ir.IntType(8))])
        print_s_fn = ir.Function(self.module, print_s_ty, "print_str")
        self.functions["print_str"] = print_s_fn
        input_ty = ir.FunctionType(ir.IntType(32), [])
        input_fn = ir.Function(self.module, input_ty, "input_int")
        self.functions["input"] = input_fn

    def emit_program(self, ast):
        structs = []
        imports = []
        funcdefs = []
        toplevel = []
        for node in ast:
            if isinstance(node, StructDef):
                structs.append(node)
            elif isinstance(node, Import):
                imports.append(node)
            elif isinstance(node, FuncDef):
                funcdefs.append(node)
            else:
                toplevel.append(node)

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

        for node in funcdefs:
            self.emit(node)

        if toplevel:
            main = ir.Function(self.module, ir.FunctionType(ir.IntType(32), []), "main")
            entry = main.append_basic_block("entry")
            self.builder = ir.IRBuilder(entry)
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
        if node.size is not None:
            count = self.emit(node.size)
            # Convert count to i64 if needed
            if count.type != _i64:
                count = self.builder.zext(count, _i64)
        else:
            count = ir.Constant(_i64, 1)
        elem_ty = self.llvm_type(node.type_expr)
        if node.type_expr.endswith('[]'):
            elem_ty = self.llvm_type(node.type_expr[:-2])
        malloc_ty = ir.PointerType(elem_ty)
        elem_size = self._sizeof_type(elem_ty)
        total_size = self.builder.mul(count, elem_size)
        malloc_fn = self._get_malloc_fn()
        ptr = self.builder.call(malloc_fn, [total_size])
        ptr = self.builder.bitcast(ptr, malloc_ty)
        return ptr

    def _get_malloc_fn(self):
        for f in self.module.functions:
            if f.name == 'malloc':
                return f
        fnty = ir.FunctionType(_i8ptr, [_i64])
        return ir.Function(self.module, fnty, 'malloc')

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
        return ir.Constant(_i64, sz)

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
                ptr = self.builder.alloca(ssa.type, name=name)
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
                actual = elem_tys[field_idx]
                for n, t in self._all_named_types():
                    if t == actual:
                        return n
        return None

    def _all_named_types(self):
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
                ptr = self.builder.alloca(ssa.type, name=name)
                self.builder.store(ssa, ptr)
                self.locals[name] = ptr
                return ptr
            raise Exception(f"Undefined variable '{name}'")
        if isinstance(node, Index):
            obj = self.emit(node.obj)
            idx = self.emit(node.index)
            return self.builder.gep(obj, [idx], inbounds=True)
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
        ret_ty = self.llvm_type(getattr(node, 'rettype', None) or 'int')
        param_tys = [self.llvm_type(t) for t in node.params.values()]
        func = ir.Function(
            self.module,
            ir.FunctionType(ret_ty, param_tys),
            name=node.name
        )

        self.functions[node.name] = func

        entry = func.append_basic_block("entry")
        self.builder = ir.IRBuilder(entry)

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
        if node.value is not None:
            value = self.emit(node.value)
            self.builder.ret(value)
        else:
            self.builder.ret_void()
        return None

    def emit_if(self, node: If):
        cond = self.emit(node.cond)
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
            self.emit(stmt)
        if not self.builder.block.is_terminated:
            self.builder.branch(end_bb)

        if node.orelse:
            self.builder.position_at_end(else_bb)
            for stmt in node.orelse:
                self.emit(stmt)
            if not self.builder.block.is_terminated:
                self.builder.branch(end_bb)

        self.builder.position_at_end(end_bb)

    def _promote(self, left, right):
        if isinstance(left.type, ir.DoubleType) and not isinstance(right.type, ir.DoubleType):
            right = self.builder.sitofp(right, ir.DoubleType())
        elif isinstance(right.type, ir.DoubleType) and not isinstance(left.type, ir.DoubleType):
            left = self.builder.sitofp(left, ir.DoubleType())
        # Handle int64 promotion
        elif isinstance(left.type, ir.IntType) and isinstance(right.type, ir.IntType):
            if left.type.width == 64 and right.type.width == 32:
                right = self.builder.zext(right, ir.IntType(64))
            elif right.type.width == 64 and left.type.width == 32:
                left = self.builder.zext(left, ir.IntType(64))
        return left, right

    def emit_binop(self, node):
        if node.op == TokenType.PLUS and self._is_string_concat(node):
            return self._emit_string_concat(node)

        match node.op:
            case TokenType.AND:
                lhs = self.emit(node.left)
                res_ty = lhs.type
                entry_bb = self.builder.block
                rhs_bb = self.builder.append_basic_block("and.rhs")
                end_bb = self.builder.append_basic_block("and.end")
                is_true = self.builder.icmp_signed('!=', lhs, ir.Constant(res_ty, 0))
                self.builder.cbranch(is_true, rhs_bb, end_bb)
                self.builder.position_at_end(rhs_bb)
                rhs = self.emit(node.right)
                self.builder.branch(end_bb)
                self.builder.position_at_end(end_bb)
                phi = self.builder.phi(res_ty)
                phi.add_incoming(lhs, entry_bb)
                phi.add_incoming(rhs, rhs_bb)
                return phi

            case TokenType.OR:
                lhs = self.emit(node.left)
                res_ty = lhs.type
                entry_bb = self.builder.block
                rhs_bb = self.builder.append_basic_block("or.rhs")
                end_bb = self.builder.append_basic_block("or.end")
                is_true = self.builder.icmp_signed('!=', lhs, ir.Constant(res_ty, 0))
                self.builder.cbranch(is_true, end_bb, rhs_bb)
                self.builder.position_at_end(rhs_bb)
                rhs = self.emit(node.right)
                self.builder.branch(end_bb)
                self.builder.position_at_end(end_bb)
                phi = self.builder.phi(res_ty)
                phi.add_incoming(lhs, entry_bb)
                phi.add_incoming(rhs, rhs_bb)
                return phi

        left = self.emit(node.left)
        right = self.emit(node.right)
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
                return self.builder.add(left, right, flags=['nsw'])
            case TokenType.MINUS:
                return self.builder.sub(left, right, flags=['nsw'])
            case TokenType.STAR:
                return self.builder.mul(left, right, flags=['nsw'])
            case TokenType.SLASH:
                return self.builder.sdiv(left, right)
            case TokenType.SLASH_SLASH:
                return self.builder.sdiv(left, right)
            case TokenType.PERCENT:
                return self.builder.srem(left, right)
            case TokenType.SHL:
                return self.builder.shl(left, right)
            case TokenType.SHR:
                return self.builder.ashr(left, right)
            case TokenType.AMPERSAND:
                return self.builder.and_(left, right)
            case TokenType.PIPE:
                return self.builder.or_(left, right)
            case TokenType.CARET:
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

    def _normalize_ptr_cmp(self, left, right):
        if isinstance(left.type, ir.PointerType) and isinstance(right.type, ir.IntType):
            return left, ir.Constant(left.type, None)
        if isinstance(right.type, ir.PointerType) and isinstance(left.type, ir.IntType):
            return ir.Constant(right.type, None), right
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
        new_str = self.builder.call(malloc_fn, [plus_one])
        self.builder.call(memcpy_fn, [new_str, left, left_len])
        dest_plus = self.builder.gep(new_str, [left_len], inbounds=True)
        self.builder.call(memcpy_fn, [dest_plus, right, right_len])
        null_byte = self.builder.gep(new_str, [total_len], inbounds=True)
        self.builder.store(ir.Constant(_i8, 0), null_byte)
        return new_str

    def _get_strlen_fn(self):
        for f in self.module.functions:
            if f.name == 'strlen':
                return f
        fnty = ir.FunctionType(_i32, [_i8ptr])
        return ir.Function(self.module, fnty, 'strlen')

    def _get_memcpy_fn(self):
        for f in self.module.functions:
            if f.name == 'memcpy':
                return f
        fnty = ir.FunctionType(_i8ptr, [_i8ptr, _i8ptr, _i32])
        return ir.Function(self.module, fnty, 'memcpy')

    def emit_unary(self, node: UnaryOp):
        match node.op:
            case TokenType.PLUS:
                return self.emit(node.operand)
            case TokenType.MINUS:
                value = self.emit(node.operand)
                zero = ir.Constant(value.type, 0.0 if isinstance(value.type, ir.DoubleType) else 0)
                if isinstance(value.type, ir.DoubleType):
                    return self.builder.fsub(zero, value)
                return self.builder.sub(zero, value)
            case TokenType.TILDE:
                value = self.emit(node.operand)
                all_ones = ir.Constant(value.type, -1)
                return self.builder.xor(value, all_ones)
            case TokenType.NOT:
                value = self.emit(node.operand)
                zero = ir.Constant(value.type, 0)
                return self.builder.icmp_unsigned('==', value, zero)

    def emit_variable(self, node):
        ptr = self.locals.get(node.name)
        if ptr is not None:
            return self.builder.load(ptr, node.name)
        ssa = self.ssa_values.get(node.name)
        if ssa is not None:
            return ssa
        raise Exception(f"Undefined variable '{node.name}' at L{node._token.line}:{node._token.column}")

    def emit_assign(self, node):
        if isinstance(node.target, Variable):
            name = node.target.name
            ptr = self.locals.get(name)
            if ptr is not None:
                value = self.emit(node.value)
                # Handle implicit int to int64 conversion
                var_type = self.local_types.get(name)
                if var_type in ('int64', 'uint64') and value.type == ir.IntType(32):
                    value = self.builder.zext(value, ir.IntType(64))
                # Handle implicit int64/uint64 conversions (no-op since both are i64)
                self.builder.store(value, ptr)
                return None
            value = self.emit(node.value)
            ssa = self.ssa_values.pop(name, None)
            ptr = self.builder.alloca(value.type, name=name)
            self.locals[name] = ptr
            if ssa is not None:
                self.builder.store(ssa, ptr)
            self.builder.store(value, ptr)
            return None
        if isinstance(node.target, str):
            name = node.target
            ptr = self.locals.get(name)
            if ptr is not None:
                value = self.emit(node.value)
                # Handle implicit int to int64 conversion
                var_type = self.local_types.get(name)
                if var_type in ('int64', 'uint64') and value.type == ir.IntType(32):
                    value = self.builder.zext(value, ir.IntType(64))
                self.builder.store(value, ptr)
                return None
            value = self.emit(node.value)
            ssa = self.ssa_values.pop(name, None)
            ptr = self.builder.alloca(value.type, name=name)
            self.locals[name] = ptr
            self.local_types[name] = str(value.type)
            if ssa is not None:
                self.builder.store(ssa, ptr)
            self.builder.store(value, ptr)
            return None

        target_ptr = self._emit_lvalue(node.target)
        value = self.emit(node.value)
        if isinstance(target_ptr.type.pointee, ir.PointerType) and isinstance(value.type, ir.IntType):
            value = ir.Constant(target_ptr.type.pointee, None)
        self.builder.store(value, target_ptr)
        return None

    def emit_call(self, node):
        func = self.functions.get(node.callee.name)
        if func is None:
            raise Exception(f"Undefined function '{node.callee.name}' at L{node._token.line}:{node._token.column}")
        args = [self.emit(arg) for arg in node.args]
        return self.builder.call(func, args)

    def emit_print(self, node):
        value = self.emit(node.value)
        if isinstance(value.type, ir.DoubleType):
            return self.builder.call(self.functions["print_double"], [value])
        if (isinstance(value.type, ir.PointerType)
                and isinstance(value.type.pointee, ir.IntType)
                and value.type.pointee.width == 8):
            return self.builder.call(self.functions["print_str"], [value])
        # Handle int64 values by truncating to int32 for printing
        if isinstance(value.type, ir.IntType) and value.type.width == 64:
            value = self.builder.trunc(value, ir.IntType(32))
        return self.builder.call(self.functions["print_int"], [value])

    def emit_input(self, node):
        func = self.functions["input"]
        return self.builder.call(func, [])

    def emit_while(self, node):
        cond_bb = self.builder.append_basic_block("while.cond")
        body_bb = self.builder.append_basic_block("while.body")
        end_bb = self.builder.append_basic_block("while.end")

        self.builder.branch(cond_bb)
        self.builder.position_at_end(cond_bb)

        cond = self.emit(node.cond)
        self.builder.cbranch(cond, body_bb, end_bb)
        self.builder.position_at_end(body_bb)

        for stmt in node.body:
            self.emit(stmt)

        if not self.builder.block.is_terminated:
            self.builder.branch(cond_bb)

        self.builder.position_at_end(end_bb)

    def emit_number(self, node):
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
        val = node.value + '\0'
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

    def emit_var_decl(self, node):
        ty = self.llvm_type(node.var_type)
        ptr = self.builder.alloca(ty, name=node.name)
        self.locals[node.name] = ptr
        self.local_types[node.name] = node.var_type
        if node.init:
            value = self.emit(node.init)
            # Handle implicit int to int64 conversion
            if node.var_type in ('int64', 'uint64') and value.type == ir.IntType(32):
                value = self.builder.zext(value, ir.IntType(64))
            # Handle implicit int64/uint64 conversions (no-op since both are i64)
            self.builder.store(value, ptr)
        elif isinstance(ty, ir.PointerType):
            self.builder.store(ir.Constant(ty, None), ptr)

    def emit_import(self, node):
        for fname, (ret_type, params, vararg) in node.symbols:
            if fname in self.functions:
                continue
            ret_ty = self.llvm_type(ret_type)
            param_tys = [self.llvm_type(t) for _, t in params]
            fnty = ir.FunctionType(ret_ty, param_tys, var_arg=vararg)
            func = ir.Function(self.module, fnty, name=fname)
            self.functions[fname] = func
        if node.src_file:
            self.import_src_files.append(node.src_file)
