import sys
import re
import os
from llvmlite import ir, binding

# --- NEW: SEARCH_PATH Definition ---
# This tells the compiler where to look for <library.ax> files.
# It defaults to the current working directory.
SEARCH_PATH = os.getcwd()

def compile_ax_smart(filename):
    # Read source
    if not os.path.exists(filename):
        raise FileNotFoundError(f"File not found: {filename}")
        
    with open(filename, 'r') as f:
        raw_content = f.read()
    
    clean_content = re.sub(r'//.*?;', '', raw_content, flags=re.DOTALL)

    lines = [line.rstrip() for line in clean_content.splitlines() if line.strip()]

    if not lines:
        raise ValueError("Empty source file after stripping comments")

    # Initialize LLVM
    binding.initialize()
    binding.initialize_native_target()
    binding.initialize_native_asmprinter()
    target = binding.get_default_triple()

    module = ir.Module(name="smart_module")
    module.triple = target
    target_machine = binding.Target.from_default_triple().create_target_machine()
    module.data_layout = target_machine.target_data

    # printf declaration
    printf_ty = ir.FunctionType(ir.IntType(32), [ir.PointerType(ir.IntType(8))], var_arg=True)
    printf = ir.Function(module, printf_ty, name="printf")

    # Format strings
    fmt_i8 = ir.GlobalVariable(module, ir.ArrayType(ir.IntType(8), 4), name="fmt_i8")
    fmt_i8.global_constant = True
    fmt_i8.linkage = 'internal'
    fmt_i8.initializer = ir.Constant(ir.ArrayType(ir.IntType(8), 4), bytearray("%d\n\0", "utf8"))

    fmt_f8 = ir.GlobalVariable(module, ir.ArrayType(ir.IntType(8), 4), name="fmt_f8")
    fmt_f8.global_constant = True
    fmt_f8.linkage = 'internal'
    fmt_f8.initializer = ir.Constant(ir.ArrayType(ir.IntType(8), 4), bytearray("%f\n\0", "utf8"))

    fmt_c8 = ir.GlobalVariable(module, ir.ArrayType(ir.IntType(8), 4), name="fmt_c8")
    fmt_c8.global_constant = True
    fmt_c8.linkage = 'internal'
    fmt_c8.initializer = ir.Constant(ir.ArrayType(ir.IntType(8), 4), bytearray("%c\n\0", "utf8"))

    # Global State Trackers
    functions = {} # Maps fname -> (ir.Function, ret_type_str, [arg_type_strs])
    builder = None
    symbols = {}
    dropped_vars = set()
    block_stack = []
    current_func_ret_type = None

    def get_llvm_type(t_str):
        if t_str == "void": return ir.VoidType()
        if t_str in ["i8", "i16", "i32", "i64"]: return ir.IntType(int(t_str[1:]))
        if t_str in ["c8", "c16", "c32", "c64"]: return ir.IntType(int(t_str[1:]))
        if t_str == "bool": return ir.IntType(1)
        if t_str in ["f8", "f64"]: return ir.DoubleType()
        if t_str == "f16": return ir.HalfType()
        if t_str == "f32": return ir.FloatType()
        
        # Recursive array parsing support
        if t_str.startswith("[") and "]" in t_str and t_str.count("[") == 1:
            size = int(t_str[1:t_str.index("]")])
            base_type = t_str[t_str.index("]")+1:]
            return ir.ArrayType(get_llvm_type(base_type), size)
            
        raise ValueError(f"Unsupported type: '{t_str}'")

# --- PATCH: scan_file_for_signatures ---
    def scan_file_for_signatures(path):
        """
        Pass 1: Only extracts 'fn' labels from external files.
        """
        # If path is provided without extension, default to .axm
        if not os.path.exists(path):
            if os.path.exists(path + ".axm"):
                path += ".axm"
            elif os.path.exists(path + ".ax"):
                path += ".ax"
            else:
                raise FileNotFoundError(f"Import Error: Could not find '{path}'")
        with open(path, 'r') as f:
            for line in f:
                line = line.strip()
                if line.startswith("fn ") and line.endswith(":"):
                    # FIXED REGEX: Changed [A-Za-0-9] to [A-Za-z0-9]
                    match = re.match(r"fn\s+([A-Za-z_]\w*)\s*\((.*)\)\s*->\s*([A-Za-z0-9_\[\]]+)", line[:-1])
                    if match:
                        fname, args_s, ret_s = match.groups()
                        if fname in functions: continue 
                        
                        a_types, a_type_strs = [], []
                        if args_s:
                            for arg in args_s.split(","):
                                _, atype = arg.split(":")
                                a_type_strs.append(atype.strip())
                                a_types.append(get_llvm_type(atype.strip()))
                        
                        f_ty = ir.FunctionType(get_llvm_type(ret_s), a_types)
                        f_ext = ir.Function(module, f_ty, name=fname)
                        functions[fname] = (f_ext, ret_s, a_type_strs)

# --- PASS 1: PRE-SCAN IMPORTS AND LOCAL SIGNATURES ---
    for line in lines:
        stripped = line.strip()
        
        # 1. Harvest Imports
        import_match = re.match(r"import\s+<(.+?)>;", stripped)
        if import_match:
            lib_base = import_match.group(1)
            # Prefer .axm over .ax
            full_path = os.path.join(SEARCH_PATH, f"{lib_base}.axm")
            if not os.path.exists(full_path):
                full_path = os.path.join(SEARCH_PATH, f"{lib_base}.ax")
            scan_file_for_signatures(full_path)
            
        # 2. Harvest Local Function Signatures (Forward Declaration Fix)
        if stripped.startswith("fn ") and stripped.endswith(":"):
            match = re.match(r"fn\s+([A-Za-z_]\w*)\s*\((.*)\)\s*->\s*([A-Za-z0-9_\[\]]+)", stripped[:-1])
            if match:
                fname, args_s, ret_s = match.groups()
                
                # Only register if we haven't seen it (prevents overwriting imports)
                if fname not in functions:
                    a_types, a_type_strs = [], []
                    if args_s:
                        for arg in args_s.split(","):
                            _, atype = arg.split(":")
                            a_type_strs.append(atype.strip())
                            a_types.append(get_llvm_type(atype.strip()))
                    
                    f_ty = ir.FunctionType(get_llvm_type(ret_s), a_types)
                    f_ext = ir.Function(module, f_ty, name=fname)
                    # Add to global functions dictionary so the parser knows it exists!
                    functions[fname] = (f_ext, ret_s, a_type_strs)

    # --- PASS 2: MAIN COMPILATION ---
    lines_to_process = lines[:]
    while lines_to_process:
        line = lines_to_process.pop(0)
        stripped = line.strip()
        if not stripped: continue

        # --- PATCH: SKIP IMPORTS IN PASS 2 ---
        if stripped.startswith("import "):
            continue

        # ... [Rest of your 800+ lines of loop logic] ...
    def build_syscall(builder, args):
            """
            Generates inline assembly for x86_64 Linux syscalls.
            args[0] = syscall ID (rax)
            args[1..6] = arguments (rdi, rsi, rdx, r10, r8, r9)
            """
            num_args = len(args) - 1
            if num_args < 0 or num_args > 6:
                raise ValueError("syscall takes 1 to 7 arguments (id + up to 6 args)")
            
            # Ensure all arguments are 64-bit integers for the registers
            casted_args = []
            for a in args:
                if isinstance(a.type, ir.PointerType):
                    casted_args.append(builder.ptrtoint(a, ir.IntType(64)))
                elif a.type.width < 64:
                    casted_args.append(builder.zext(a, ir.IntType(64)))
                else:
                    casted_args.append(a)
                    
            # Define the register mapping constraints for LLVM inline asm
            reg_names = ["{rax}", "{rdi}", "{rsi}", "{rdx}", "{r10}", "{r8}", "{r9}"]
            input_constraints = ",".join(reg_names[:len(casted_args)])
            
            # Output is rax. Inputs are rax + args. Clobbers rcx, r11, and memory.
            constraints = f"={{rax}},{input_constraints},~{{rcx}},~{{r11}},~{{memory}}"
            
            asm_ty = ir.FunctionType(ir.IntType(64), [ir.IntType(64)] * len(casted_args))
            asm = ir.InlineAsm(asm_ty, "syscall", constraints, side_effect=True)
            
            return builder.call(asm, casted_args)

    def evaluate_expr(expr, builder, symbols, dropped_vars, expected_type_str):
        """
        Evaluate an expression. 
        Integrated with Syscalls, '@' (Copy), '&' (Address), and '$' (Physical Access).
        """
        expr = expr.strip()
        
        # --- Intercept Pure Function Calls & Syscalls ---
        match = re.match(r"^([A-Za-z_]\w*)\s*\((.*)\)$", expr)
        if match:
            fname = match.group(1)
            args_str = match.group(2).strip()

            # --- NEW: Syscall Intercept ---
            if fname == "syscall":
                call_args = []
                if args_str:
                    arg_tokens = [a.strip() for a in args_str.split(",")]
                    for atok in arg_tokens:
                        # Evaluate all syscall args as 64-bit integers for x86_64 registers
                        val = evaluate_expr(atok, builder, symbols, dropped_vars, "i64")
                        call_args.append(val)
                
                result = build_syscall(builder, call_args)
                
                # Cast return value (rax) to expected type if assigned to a variable
                if expected_type_str != "void":
                    expected_llvm_type = get_llvm_type(expected_type_str)
                    if result.type != expected_llvm_type:
                        # Rax is 64-bit; truncate if assigning to smaller types
                        if expected_llvm_type.width < 64:
                            result = builder.trunc(result, expected_llvm_type)
                return result

            # --- Existing Function Call Logic ---
            if fname in functions:
                func, ret_type, arg_types = functions[fname]
                
                if ret_type == "void" and expected_type_str != "void":
                    raise ValueError(f"Type Error: Cannot assign void function '{fname}' to '{expected_type_str}'.")
                    
                call_args = []
                if args_str:
                    arg_tokens = [a.strip() for a in args_str.split(",")]
                    for i, atok in enumerate(arg_tokens):
                        val = evaluate_expr(atok, builder, symbols, dropped_vars, arg_types[i])
                        call_args.append(val)
                result = builder.call(func, call_args)
                
                # Simple Casting for returns
                if expected_type_str != "void":
                    expected_llvm_type = get_llvm_type(expected_type_str)
                    if not expected_type_str.startswith('['):
                        if result.type != expected_llvm_type:
                            if isinstance(result.type, ir.IntType): 
                                result = builder.sext(result, expected_llvm_type)
                            else: 
                                result = builder.fpext(result, expected_llvm_type)
                return result

        # --- Tokenizer (Supports hardware $, copy @, address &) ---
        tokens = re.findall(r"'[^']*'|\"[^\"]*\"|[@&$]*[A-Za-z_]\w*|\d+(?:\.\d+)?|&&|\|\||!=|==|<=|>=|<|>|\^\||<<|>>|[+\-*/]", expr)
        if not tokens:
            raise ValueError("Empty expression")

        is_float = expected_type_str.startswith('f')
        is_array = expected_type_str.startswith('[')
        llvm_type = get_llvm_type(expected_type_str)

        result = None
        current_op = None
        apply_not = False

        for t in tokens:
            if t == "!=" and result is None:
                apply_not = True
                continue
            if t in ["+", "-", "*", "/", "&&", "||", "^|", "<<", ">>", "==", "!=", "<", ">", "<=", ">="]:
                current_op = t
                continue

            # --- Literals ---
            if (t.startswith("'") and t.endswith("'")) or (t.startswith('"') and t.endswith('"')):
                # Evaluate escape sequences correctly
                raw_str = t.strip("'\"")
                raw_str = raw_str.replace('\\n', '\n').replace('\\t', '\t').replace('\\0', '\x00')
                
                clean_str = raw_str.encode('utf-8').ljust(llvm_type.width // 8, b'\0')
                val = ir.Constant(llvm_type, int.from_bytes(clean_str, byteorder="little"))
            elif re.match(r'^\d+(?:\.\d+)?$', t):
                val = ir.Constant(llvm_type, float(t) if is_float else int(t))
            elif t in ["true", "false"]:
                val = ir.Constant(llvm_type, 1 if t == "true" else 0)

            # --- Variables & Hardware ($) ---
            else:
                is_hw = '$' in t
                is_copy = '@' in t
                is_addr_of = '&' in t
                var_name = t.lstrip('@&$') # Cleanly strip all prefixes

                if var_name not in symbols:
                    raise ValueError(f"Unknown variable: '{var_name}'")
                if var_name in dropped_vars:
                    raise ValueError(f"Ownership Error: Cannot use moved/dropped variable '{var_name}'.")

                ptr, typ, is_const = symbols[var_name]

                if is_addr_of:
                    # Direct memory referencing (e.g., passing string array pointer to sys_write)
                    val = builder.ptrtoint(ptr, llvm_type)
                elif is_hw:
                    # Physical Load from Silicon
                    addr_val = builder.load(ptr)
                    if not is_copy and not is_const: 
                        dropped_vars.add(var_name)
                    
                    # Convert to Hardware Pointer
                    hw_ptr = builder.inttoptr(addr_val, ir.PointerType(ir.IntType(8)))
                    val = builder.load(hw_ptr)
                else:
                    # Standard Stack Load
                    val = builder.load(ptr)
                    if not is_copy and not is_const: 
                        dropped_vars.add(var_name)

                # Automatic Casting to match expected_type
                if not is_array:
                    if is_float and isinstance(val.type, ir.IntType): 
                        val = builder.sitofp(val, llvm_type)
                    elif not is_float and isinstance(val.type, (ir.FloatType, ir.DoubleType)): 
                        val = builder.fptosi(val, llvm_type)
                    elif val.type.width < llvm_type.width: 
                        val = builder.sext(val, llvm_type)
                    elif val.type.width > llvm_type.width: 
                        val = builder.trunc(val, llvm_type)

            if apply_not:
                val = builder.not_(val)
                apply_not = False

            if result is None:
                result = val
            else:
                if current_op == "+": result = builder.fadd(result, val) if is_float else builder.add(result, val)
                elif current_op == "-": result = builder.fsub(result, val) if is_float else builder.sub(result, val)
                elif current_op == "*": result = builder.fmul(result, val) if is_float else builder.mul(result, val)
                elif current_op == "/": result = builder.fdiv(result, val) if is_float else builder.sdiv(result, val)
                elif current_op == "&&": result = builder.and_(result, val)
                elif current_op == "||": result = builder.or_(result, val)
                elif current_op == "^|": result = builder.xor(result, val)
                elif current_op == "<<": result = builder.shl(result, val)
                elif current_op == ">>": result = builder.ashr(result, val)
                elif current_op in ["==", "!=", "<", ">", "<=", ">="]:
                    res = builder.fcmp_ordered(current_op, result, val) if is_float else builder.icmp_signed(current_op, result, val)
                    result = builder.zext(res, llvm_type) if llvm_type.width != 1 else res
                current_op = None
                
        return result

    # --- PASS 2: MAIN COMPILATION ---
    lines_to_process = lines[:]
    while lines_to_process:
        line = lines_to_process.pop(0)
        stripped = line.strip()
        if not stripped:
            continue

        # Skip Import Statements (Handled in Pass 1)
        if stripped.startswith("import "):
            continue

        if not stripped.startswith("fn ") and not stripped.endswith(";") and not stripped.endswith(":"):
            raise ValueError(f"Syntax Error: Missing semicolon at end of line: '{line}'")
            
        # Check inline command for control flow
        inline_cmd = None
        is_control_flow = (stripped.startswith("if (") or 
                           stripped.startswith("ifelse (") or 
                           stripped.startswith("match (") or 
                           stripped.startswith("while (") or 
                           stripped.startswith("for (") or 
                           stripped == "else:" or 
                           stripped.startswith("else:"))
        
        if is_control_flow and not stripped.endswith(":"):
            last_colon = stripped.rfind(":")
            if last_colon != -1:
                inline_cmd = stripped[last_colon+1:].strip()
                stripped = stripped[:last_colon+1]
                if inline_cmd and not inline_cmd.endswith(";"):
                    inline_cmd += ";"

        line_content = stripped[:-1].strip() if stripped.endswith(";") else stripped

        # -----------------------------
        # Function Declarations
        # -----------------------------
        if line_content.startswith("fn ") and line_content.endswith(":"):
            match = re.match(r"fn\s+([A-Za-z_]\w*)\s*\((.*)\)\s*->\s*([A-Za-z0-9_\[\]]+):", line_content)
            if not match:
                raise ValueError(f"Syntax Error: Invalid function signature: '{line_content}'")
                
            func_name = match.group(1)
            args_str = match.group(2).strip()
            ret_type_str = match.group(3).strip()
            
            arg_names = []
            arg_types = []
            arg_type_strs = []
            
            if args_str:
                for arg_decl in args_str.split(","):
                    aname, atype = arg_decl.split(":")
                    aname = aname.strip()
                    atype = atype.strip()
                    arg_names.append(aname)
                    arg_types.append(get_llvm_type(atype))
                    arg_type_strs.append(atype)
                    
            # Use existing declaration if harvested during import, otherwise create new
            if func_name in functions:
                func = functions[func_name][0]
            else:
                ret_type = get_llvm_type(ret_type_str)
                func_type = ir.FunctionType(ret_type, arg_types)
                func = ir.Function(module, func_type, name=func_name)
                functions[func_name] = (func, ret_type_str, arg_type_strs)
            
            block = func.append_basic_block(name="entry")
            builder = ir.IRBuilder(block)
            
            # Reset Environment for Local Scope
            symbols = {}
            dropped_vars = set()
            block_stack = []
            current_func_ret_type = ret_type_str
            
            # Allocate local memory for arguments passed in
            for i, aname in enumerate(arg_names):
                alloca = builder.alloca(arg_types[i], name=aname)
                builder.store(func.args[i], alloca)
                symbols[aname] = (alloca, arg_type_strs[i], False)
                
            continue
            
        # Ensure we are currently inside a function block
        if builder is None:
            raise ValueError(f"Scope Error: Statement outside of function: '{line_content}'")

        # -----------------------------
        # Standalone Function Calls & Syscalls
        # -----------------------------
        match = re.match(r"^([A-Za-z_]\w*)\s*(?:\((.*)\))?$", line_content)
        if match and (match.group(1) in functions or match.group(1) == "syscall") and not line_content.startswith("print(") and not line_content.startswith("drop("):
            fname = match.group(1)
            args_str = match.group(2)
            args_str = args_str.strip() if args_str else ""
            
            # --- NEW: Standalone Syscall ---
            if fname == "syscall":
                call_args = []
                if args_str:
                    arg_tokens = [a.strip() for a in args_str.split(",")]
                    for atok in arg_tokens:
                        # Syscalls natively expect 64-bit integers for x86_64 registers
                        val = evaluate_expr(atok, builder, symbols, dropped_vars, "i64")
                        call_args.append(val)
                
                build_syscall(builder, call_args)
                continue

            # --- Existing Function Logic ---
            func, ret_type, arg_types = functions[fname]
            
            call_args = []
            if args_str:
                arg_tokens = [a.strip() for a in args_str.split(",")]
                if len(arg_tokens) != len(arg_types):
                    raise ValueError(f"Argument count mismatch for '{fname}'")
                for i, atok in enumerate(arg_tokens):
                    val = evaluate_expr(atok, builder, symbols, dropped_vars, arg_types[i])
                    call_args.append(val)
            elif len(arg_types) > 0:
                 raise ValueError(f"Argument count mismatch for '{fname}'. Expected {len(arg_types)}, got 0")
                    
            builder.call(func, call_args)
            continue

        # -----------------------------
        # Drop (Memory Wipe & Graveyard)
        # -----------------------------
        if line_content.startswith("drop(") and line_content.endswith(")"):
            var_name = line_content[5:-1].strip()
            
            if var_name not in symbols:
                raise ValueError(f"Unknown variable: '{var_name}'")
            if var_name in dropped_vars:
                raise ValueError(f"Ownership Error: '{var_name}' is already dropped or moved.")
                
            var_ptr, var_type, is_const = symbols[var_name]
            
            if not var_type.startswith("[") and not is_const:
                if var_type in ["i8", "i16", "i32", "i64", "c8", "c16", "c32", "c64", "bool"]:
                    width = 1 if var_type == "bool" else int(var_type[1:])
                    llvm_type = ir.IntType(width)
                    builder.store(ir.Constant(llvm_type, 0), var_ptr)
                elif var_type in ["f8", "f16", "f32", "f64"]:
                    if var_type in ["f8", "f64"]: llvm_type = ir.DoubleType()
                    elif var_type == "f32": llvm_type = ir.FloatType()
                    elif var_type == "f16": llvm_type = ir.HalfType()
                    builder.store(ir.Constant(llvm_type, 0.0), var_ptr)
                    
            dropped_vars.add(var_name)
            continue

        # -----------------------------
        # Control Flow & Loop Blocks
        # -----------------------------
        if line_content == "end":
            if not block_stack:
                raise ValueError("Unexpected 'end;'")
            top = block_stack.pop()
            
            if top['type'] == 'while':
                if not builder.block.is_terminated:
                    builder.branch(top['cond_block'])
                builder.position_at_end(top['end_block'])
                
            elif top['type'] == 'for':
                if not builder.block.is_terminated:
                    builder.branch(top['inc_block'])
                    
                builder.position_at_end(top['inc_block'])
                
                inc_stmt = top['inc_stmt']
                if inc_stmt.endswith("++"):
                    vname = inc_stmt[:-2].strip()
                    ptr, typ, _ = symbols[vname]
                    val = builder.load(ptr)
                    one = ir.Constant(val.type, 1)
                    new_val = builder.add(val, one)
                    builder.store(new_val, ptr)
                elif inc_stmt.endswith("--"):
                    vname = inc_stmt[:-2].strip()
                    ptr, typ, _ = symbols[vname]
                    val = builder.load(ptr)
                    one = ir.Constant(val.type, 1)
                    new_val = builder.sub(val, one)
                    builder.store(new_val, ptr)
                    
                builder.branch(top['cond_block'])
                builder.position_at_end(top['end_block'])
                
            else: 
                if not builder.block.is_terminated:
                    builder.branch(top['end_block'])
                    
                if top['type'] in ['if', 'ifelse']:
                    builder.position_at_end(top['false_block'])
                    if not builder.block.is_terminated:
                        builder.branch(top['end_block'])
                        
                builder.position_at_end(top['end_block'])
            continue

        if line_content.startswith("while (") and line_content.endswith(":"):
            cond_expr = line_content[7:-1].strip()
            if cond_expr.endswith(")"): cond_expr = cond_expr[:-1]
            
            while_cond = builder.append_basic_block("while_cond")
            while_body = builder.append_basic_block("while_body")
            while_end = builder.append_basic_block("while_end")
            
            builder.branch(while_cond)
            builder.position_at_end(while_cond)
            
            first_var_match = re.search(r'[@&]?[A-Za-z_]\w*', cond_expr)
            expr_type = "bool"
            if first_var_match:
                vname = first_var_match.group(0).lstrip('@&')
                if vname in symbols:
                    expr_type = symbols[vname][1]
                    
            cond_val = evaluate_expr(cond_expr, builder, symbols, dropped_vars, expr_type)
            if cond_val.type.width != 1:
                cond_val = builder.trunc(cond_val, ir.IntType(1))
                
            builder.cbranch(cond_val, while_body, while_end)
            builder.position_at_end(while_body)
            
            block_stack.append({
                'type': 'while',
                'cond_block': while_cond,
                'end_block': while_end
            })
            
            if inline_cmd:
                lines_to_process.insert(0, "end;")
                lines_to_process.insert(0, inline_cmd)
            continue

        if line_content.startswith("for (") and line_content.endswith(":"):
            inner = line_content[5:-1].strip()
            if inner.endswith(")"): inner = inner[:-1]
            
            init_stmt, cond_expr, inc_stmt = [x.strip() for x in inner.split(";")]
            
            if ":" in init_stmt and "=" in init_stmt:
                var_name, rest = init_stmt.split(":", 1)
                var_type, var_value = rest.split("=", 1)
                var_name = var_name.strip()
                var_type = var_type.strip()
                var_value = var_value.strip()
                
                llvm_type = get_llvm_type(var_type)
                const_val = evaluate_expr(var_value, builder, symbols, dropped_vars, var_type)
                alloca = builder.alloca(llvm_type, name=var_name)
                builder.store(const_val, alloca)
                symbols[var_name] = (alloca, var_type, False)
                
            for_cond = builder.append_basic_block("for_cond")
            for_body = builder.append_basic_block("for_body")
            for_inc = builder.append_basic_block("for_inc")
            for_end = builder.append_basic_block("for_end")
            
            builder.branch(for_cond)
            builder.position_at_end(for_cond)
            
            first_var_match = re.search(r'[@&]?[A-Za-z_]\w*', cond_expr)
            expr_type = "bool"
            if first_var_match:
                vname = first_var_match.group(0).lstrip('@&')
                if vname in symbols:
                    expr_type = symbols[vname][1]
                    
            cond_val = evaluate_expr(cond_expr, builder, symbols, dropped_vars, expr_type)
            if cond_val.type.width != 1:
                cond_val = builder.trunc(cond_val, ir.IntType(1))
                
            builder.cbranch(cond_val, for_body, for_end)
            builder.position_at_end(for_body)
            
            block_stack.append({
                'type': 'for',
                'cond_block': for_cond,
                'inc_block': for_inc,
                'end_block': for_end,
                'inc_stmt': inc_stmt
            })
            
            if inline_cmd:
                lines_to_process.insert(0, "end;")
                lines_to_process.insert(0, inline_cmd)
            continue

        if line_content.startswith("match (") and line_content.endswith(":"):
            match_var = line_content[7:-1].strip()
            if match_var.endswith(")"): match_var = match_var[:-1]
            end_block = builder.append_basic_block("match_end")
            
            block_stack.append({
                'type': 'match',
                'var': match_var,
                'end_block': end_block
            })
            if inline_cmd:
                lines_to_process.insert(0, "end;")
                lines_to_process.insert(0, inline_cmd)
            continue

        if line_content.startswith("if (") and line_content.endswith(":"):
            cond_expr = line_content[4:-1].strip()
            if cond_expr.endswith(")"): cond_expr = cond_expr[:-1]
            
            if block_stack and block_stack[-1]['type'] == 'match':
                match_var = block_stack[-1]['var']
                if not any(op in cond_expr for op in ["==", "!=", "<", ">", "<=", ">="]):
                    cond_expr = f"{match_var} == {cond_expr}"
                    
            first_var_match = re.search(r'[@&]?[A-Za-z_]\w*', cond_expr)
            expr_type = "bool"
            if first_var_match:
                vname = first_var_match.group(0).lstrip('@&')
                if vname in symbols:
                    expr_type = symbols[vname][1]
                    
            cond_val = evaluate_expr(cond_expr, builder, symbols, dropped_vars, expr_type)
            if cond_val.type.width != 1:
                cond_val = builder.trunc(cond_val, ir.IntType(1))
                
            true_block = builder.append_basic_block("if_true")
            false_block = builder.append_basic_block("if_false")
            end_block = builder.append_basic_block("if_end")
            
            builder.cbranch(cond_val, true_block, false_block)
            builder.position_at_end(true_block)
            
            block_stack.append({
                'type': 'if',
                'end_block': end_block,
                'false_block': false_block
            })
            
            if inline_cmd:
                lines_to_process.insert(0, "end;") 
                lines_to_process.insert(0, inline_cmd)
            continue

        if line_content.startswith("ifelse (") and line_content.endswith(":"):
            cond_expr = line_content[8:-1].strip()
            if cond_expr.endswith(")"): cond_expr = cond_expr[:-1]
            
            top = block_stack[-1]
            if top['type'] not in ['if', 'ifelse']:
                raise ValueError("ifelse without preceding if")
                
            if not builder.block.is_terminated:
                builder.branch(top['end_block'])
                
            builder.position_at_end(top['false_block'])
            
            first_var_match = re.search(r'[@&]?[A-Za-z_]\w*', cond_expr)
            expr_type = "bool"
            if first_var_match:
                vname = first_var_match.group(0).lstrip('@&')
                if vname in symbols:
                    expr_type = symbols[vname][1]
                    
            cond_val = evaluate_expr(cond_expr, builder, symbols, dropped_vars, expr_type)
            if cond_val.type.width != 1:
                cond_val = builder.trunc(cond_val, ir.IntType(1))
                
            new_true = builder.append_basic_block("ifelse_true")
            new_false = builder.append_basic_block("ifelse_false")
            
            builder.cbranch(cond_val, new_true, new_false)
            builder.position_at_end(new_true)
            
            top['type'] = 'ifelse'
            top['false_block'] = new_false
            
            if inline_cmd:
                lines_to_process.insert(0, "end;")
                lines_to_process.insert(0, inline_cmd)
            continue

        if line_content == "else:":
            top = block_stack[-1]
            if not builder.block.is_terminated:
                builder.branch(top['end_block'])
                
            builder.position_at_end(top['false_block'])
            top['type'] = 'else'
            
            if inline_cmd:
                lines_to_process.insert(0, "end;")
                lines_to_process.insert(0, inline_cmd)
            continue

        # -----------------------------
        # Variable declarations
        # -----------------------------
        if ":" in line_content and "=" in line_content and "(" not in line_content.split(":")[0]:
            is_const = False
            if line_content.startswith("const "):
                is_const = True
                line_content = line_content[6:].strip()

            var_name, rest = line_content.split(":", 1)
            var_type, var_value = rest.split("=", 1)
            var_name = var_name.strip()
            var_type = var_type.strip()
            
            # THE FIX: Aggressively strip trailing semicolons and whitespace
            var_value = var_value.strip().rstrip(';')

            if var_type == "void":
                raise ValueError(f"Type Error: 'void' type cannot be instantiated as a variable.")

            # -----------------------------
            # 1D array
            # -----------------------------
            if var_type.startswith("[") and "]" in var_type and var_type.count("[") == 1:
                size = int(var_type[1:var_type.index("]")])
                base_type = var_type[var_type.index("]")+1:]

                llvm_elem_type = get_llvm_type(base_type)
                llvm_type = ir.ArrayType(llvm_elem_type, size)

                if var_value == "[]":
                    const_val = ir.Constant(llvm_type, [ir.Constant(llvm_elem_type, 0) for _ in range(size)])
                    alloca = builder.alloca(llvm_type, name=var_name)
                    builder.store(const_val, alloca)
                    symbols[var_name] = (alloca, var_type, is_const)
                elif var_value.startswith("[") and var_value.endswith("]"):
                    alloca = builder.alloca(llvm_type, name=var_name)
                    elements = [e.strip() for e in var_value[1:-1].split(",")]
                    if len(elements) < size:
                        elements += ["0"] * (size - len(elements))
                    if len(elements) != size:
                        raise ValueError(f"Array size mismatch: expected {size}, got {len(elements)}")
                    
                    for idx, e in enumerate(elements):
                        val = evaluate_expr(e, builder, symbols, dropped_vars, base_type)
                        zero = ir.Constant(ir.IntType(32), 0)
                        gep = builder.gep(alloca, [zero, ir.Constant(ir.IntType(32), idx)])
                        builder.store(val, gep)
                    
                    symbols[var_name] = (alloca, var_type, is_const)
                else:
                    match = re.match(r"^([A-Za-z_]\w*)\s*\((.*)\)$", var_value)
                    if match and match.group(1) in functions:
                        alloca = builder.alloca(llvm_type, name=var_name)
                        val = evaluate_expr(var_value, builder, symbols, dropped_vars, var_type)
                        builder.store(val, alloca)
                        symbols[var_name] = (alloca, var_type, is_const)
                    else:
                        prefix = ''
                        src_name = var_value
                        if src_name.startswith('@') or src_name.startswith('&'):
                            prefix = src_name[0]
                            src_name = src_name[1:]
                        
                        if src_name not in symbols:
                            raise ValueError(f"Unknown array: '{var_value}'")
                        if src_name in dropped_vars:
                            raise ValueError(f"Ownership Error: Cannot use moved/dropped array '{src_name}'")
                            
                        src_ptr, src_type, _ = symbols[src_name]
                        if src_type != var_type:
                             raise ValueError(f"Type mismatch: '{src_type}' vs '{var_type}'")
                             
                        alloca = builder.alloca(llvm_type, name=var_name)
                        val = builder.load(src_ptr)
                        builder.store(val, alloca)
                        
                        if prefix == '':
                            dropped_vars.add(src_name)
                            
                        symbols[var_name] = (alloca, var_type, is_const)
                
                continue 

            # -----------------------------
            # Primitive types
            # -----------------------------
            llvm_type = get_llvm_type(var_type)
            const_val = evaluate_expr(var_value, builder, symbols, dropped_vars, var_type)
            alloca = builder.alloca(llvm_type, name=var_name)
            builder.store(const_val, alloca)
            symbols[var_name] = (alloca, var_type, is_const)
            continue

        # -----------------------------
        # Array writes
        # -----------------------------
        if "[" in line_content and "]" in line_content and "=" in line_content:
            left, value_str = line_content.split("=",1)
            value_str = value_str.strip()
            var_name = left[:left.index("[")].strip()
            idxs = [int(i.strip()) for i in left[left.index("[")+1:left.rindex("]")].split("][")]
            
            if var_name not in symbols:
                raise ValueError(f"Unknown array '{var_name}'")
            if var_name in dropped_vars:
                raise ValueError(f"Ownership Error: Cannot write to moved/dropped array '{var_name}'")
                
            var_ptr, var_type, is_const = symbols[var_name]
            if is_const:
                raise ValueError(f"Cannot modify const array '{var_name}'")
                
            zero = ir.Constant(ir.IntType(32), 0)
            gep = builder.gep(var_ptr, [zero] + [ir.Constant(ir.IntType(32), i) for i in idxs])

            if var_type.startswith("["):
                base_type = var_type[var_type.index("]")+1:]
            else:
                base_type = var_type

            val = evaluate_expr(value_str, builder, symbols, dropped_vars, base_type)
            builder.store(val, gep)
            continue

        # -----------------------------
        # Primitive Reassignment & Hardware Store ($)
        # -----------------------------
        # This block catches lines like 'VAR = VALUE' or '$ADDR_VAR = VALUE'
        if "=" in line_content and ":" not in line_content and "[" not in line_content.split("=")[0]:
            left, value_str = line_content.split("=", 1)
            left_name = left.strip()
            value_str = value_str.strip()

            # --- NEW: DYNAMIC PHYSICAL STORE ($) ---
            if left_name.startswith('$'):
                addr_var = left_name.lstrip('$@&') 
                
                if addr_var not in symbols:
                    raise ValueError(f"Unknown address variable: '{addr_var}'")
                if addr_var in dropped_vars:
                    raise ValueError(f"Ownership Error: Cannot use moved/dropped address variable '{addr_var}'.")
                
                addr_ptr, _, _ = symbols[addr_var]
                actual_addr_val = builder.load(addr_ptr)
                
                # --- THE FIX: Dynamic Type Sizing ---
                # Default to i8 for Arduino hardware literals, but if the right-hand side 
                # is a known variable (like our i64 pointer), use its exact size!
                rhs_clean = value_str.lstrip('@&')
                store_type = "i8" 
                if rhs_clean in symbols:
                    store_type = symbols[rhs_clean][1]
                
                val_to_store = evaluate_expr(value_str, builder, symbols, dropped_vars, store_type)
                
                # Silicon Bridge: Convert integer address to a physical pointer of the exact target width
                hw_ptr = builder.inttoptr(actual_addr_val, ir.PointerType(val_to_store.type))
                
                builder.store(val_to_store, hw_ptr)
                continue

            # --- STANDARD STACK ASSIGNMENT ---
            # Syntax: led_on = false
            var_name = left_name
            if var_name not in symbols:
                raise ValueError(f"Unknown variable for reassignment '{var_name}'")
            
            var_ptr, var_type, is_const = symbols[var_name]
            if is_const:
                raise ValueError(f"Cannot modify const variable '{var_name}'")

            # Evaluate RHS. evaluate_expr handles move/copy logic automatically.
            val = evaluate_expr(value_str, builder, symbols, dropped_vars, var_type)
            
            # If reassigning a previously moved/dropped variable, we "resurrect" it.
            if var_name in dropped_vars:
                dropped_vars.remove(var_name)
                
            builder.store(val, var_ptr)
            continue

        # -----------------------------
        # Print
        # -----------------------------
        if line_content.startswith("print(") and line_content.endswith(")"):
            content = line_content[6:-1].strip()
            
            prefix = ''
            if content.startswith('@') or content.startswith('&'):
                prefix = content[0]
                content = content[1:]
                
            if "[" in content and "]" in content:
                var_name = content[:content.index("[")].strip()
                idxs = [int(i.strip()) for i in content[content.index("[")+1:content.rindex("]")].split("][")]
                if var_name not in symbols:
                    raise ValueError(f"Unknown array '{var_name}'")
                if var_name in dropped_vars:
                    raise ValueError(f"Ownership Error: Cannot print from moved/dropped array '{var_name}'")
                    
                var_ptr, var_type, _ = symbols[var_name]
                zero = ir.Constant(ir.IntType(32), 0)
                gep = builder.gep(var_ptr, [zero] + [ir.Constant(ir.IntType(32), i) for i in idxs])
                val = builder.load(gep)
                
                # FIX: Strip array brackets so LLVM knows the base primitive type
                if var_type.startswith("["):
                    var_type = var_type[var_type.index("]")+1:]
                
                if prefix == '':
                    dropped_vars.add(var_name)
                    
            elif content in symbols:
                if content in dropped_vars:
                    raise ValueError(f"Ownership Error: Cannot print moved/dropped variable '{content}'")
                    
                var_ptr, var_type, _ = symbols[content]
                
                if prefix == '&':
                    val = builder.ptrtoint(var_ptr, ir.IntType(64))
                    var_type = "i64"
                else:
                    val = builder.load(var_ptr)
                    if prefix == '':
                        dropped_vars.add(content)
                        
            elif content.isdigit():
                # FIX: Default to i32 so we don't overflow numbers larger than 127
                val = ir.Constant(ir.IntType(32), int(content))
                var_type = "i32"
            elif content.startswith("'") and content.endswith("'"):
                val = ir.Constant(ir.IntType(8), ord(content[1]))
                var_type = "c8"
            elif content == "true":
                val = ir.Constant(ir.IntType(1), 1)
                var_type = "bool"
            elif content == "false":
                val = ir.Constant(ir.IntType(1), 0)
                var_type = "bool"
            else:
                raise ValueError(f"Unknown variable or literal '{content}'")

            if var_type in ["i8","i16","i32","i64","bool", "c8", "c16", "c32", "c64"]:
                fmt_ptr = builder.bitcast(fmt_i8, ir.PointerType(ir.IntType(8)))
                if var_type in ["i8","i16","i32","i64"]:
                    val32 = builder.sext(val, ir.IntType(64)) if var_type=="i64" else builder.sext(val, ir.IntType(32))
                elif var_type in ["c8", "c16", "c32", "c64"]:
                    val32 = builder.sext(val, ir.IntType(32))
                elif var_type=="bool":
                    val32 = builder.zext(val, ir.IntType(32))
                builder.call(printf,[fmt_ptr,val32])
            elif var_type in ["f8", "f16", "f32", "f64"]:
                fmt_ptr = builder.bitcast(fmt_f8, ir.PointerType(ir.IntType(8)))
                builder.call(printf,[fmt_ptr,val])
            continue

        # -----------------------------
        # Function Scope Return
        # -----------------------------
        if line_content.startswith("return"):
            if line_content == "return" or line_content == "return;":
                if current_func_ret_type != "void":
                    raise ValueError(f"Type Error: Function expects '{current_func_ret_type}' but returned void.")
                builder.ret_void()
            else:
                if current_func_ret_type == "void":
                    raise ValueError("Type Error: Void functions cannot return a value.")
                
                ret_val_str = line_content[6:].strip()
                if ret_val_str.endswith(";"): ret_val_str = ret_val_str[:-1]
                
                val = evaluate_expr(ret_val_str.strip(), builder, symbols, dropped_vars, current_func_ret_type)
                builder.ret(val)
            continue
            
        raise ValueError(f"Syntax Error: Unknown instruction: '{line_content}'")

    return str(module)

if __name__=="__main__":
    if len(sys.argv)!=2:
        print("Usage: python axiom_compiler.py program.ax")
        sys.exit(1)
    llvm_ir = compile_ax_smart(sys.argv[1])
    print(llvm_ir)
"""
Finished:
    - fn main() -> bool: return true/false;
    - data types: i8/i16/i32/i64, f8/f16/f32/f64, c8/c16/c32/c64, bool, const
    - const keyword (immutable variables)
    - Temporary print function (with literals)
    - array types i8/i16/i32/i64, f8/f16/f32/f64, c8/c16/c32/c64, bool, const (print function doesn't always work for all types)
    - math operations
    - ownership rules
    - bitwise operations
    - conditional
    - loops
    - functions with arguments which return always a requirement to mark end of scope for the function

Unfinished: (DO AN LLVM IR OPTIMIZATION AND COMPILATION CHECK FOR EACH OBJECTIVE FINISHED)
    - import

Commands:
python axiom_compiler.py program.ax > program.ll
llc -filetype=obj program.ll -o program.o / llc -filetype=obj -relocation-model=pic program.ll -o program.o
clang program.o -o program
./program

python axiom_compiler.py blink.ax > blink.ll
llc -march=avr -mcpu=atmega2560 -filetype=obj blink.ll -o blink.o
avr-gcc -mmcu=atmega2560 blink.o -o blink.elf
avr-objcopy -O ihex -R .eeprom blink.elf blink.hex
avrdude -v -p m2560 -c wiring -P /dev/ttyACM0 -b 115200 -D -U flash:w:blink.hex:i
"""