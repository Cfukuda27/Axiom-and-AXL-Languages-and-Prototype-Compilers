import sys
import re
import os

def resolve_imports(lines, base_dir, visited=None):
    if visited is None: visited = set()
    resolved = []
    for line in lines:
        if line.startswith("import "):
            match = re.match(r"import\s+<(.+?)>;", line)
            if match:
                import_path = os.path.join(base_dir, match.group(1))
                if import_path not in visited:
                    visited.add(import_path)
                    with open(import_path, 'r') as f:
                        resolved.extend(resolve_imports(f.read().splitlines(), base_dir, visited))
        else:
            resolved.append(line)
    return resolved

def compile_direct(filename):
    if not os.path.exists(filename):
        raise FileNotFoundError(f"File not found: {filename}")

    base_dir = os.path.dirname(os.path.abspath(filename))
    with open(filename, 'r') as f:
        lines = resolve_imports(f.read().splitlines(), base_dir)

    lines = [re.sub(r'//.*', '', l).strip() for l in lines if re.sub(r'//.*', '', l).strip()]

    hardware_map = {}
    instruction_map = {}
    out_asm = []

    # --- PASS 1: Read the Maps ---
    state = "GLOBAL"
    for line in lines:
        if line == "hardware_map:": state = "HW"; continue
        if state == "HW":
            if line == "end;": state = "GLOBAL"; continue
            m = re.match(r'"([A-Za-z_]\w*)"\s*=\s*"(.*?)";', line)
            if m: hardware_map[m.group(1)] = m.group(2)
            continue

        if line == "instruction_map:": state = "INST"; continue
        if state == "INST":
            if line == "end;": state = "GLOBAL"; continue
            m = re.match(r'"(.*?)"\s*=\s*"(.*?)";', line)
            if m:
                # FIX: Properly decode \n and \t for the GNU Assembler
                raw_str = m.group(2).replace("\\n", "\n").replace("\\t", "\t")
                instruction_map[m.group(1)] = raw_str
            continue

    # --- PASS 2: Macro Expansion (Direct to Target Code) ---
    state = "GLOBAL"
    for line in lines:
        if line in ["hardware_map:", "instruction_map:"]: state = "IGNORE"; continue
        if state == "IGNORE":
            if line == "end;": state = "GLOBAL"; continue
            continue

        # Labels
        if line.endswith(":"):
            out_asm.append(f"\n{line}")
            continue

        # Return token
        if line == "return;":
            out_asm.append("\t" + instruction_map.get("return", "ret"))
            continue

        # Unconditional Jump
        match_jump = re.match(r"^jump\s+([A-Za-z_]\w*);$", line)
        if match_jump:
            target = match_jump.group(1)
            asm = instruction_map["jump"].format(target=target)
            out_asm.append("\t" + asm)
            continue

        # Conditional Jump Not Zero (COMMA REMOVED)
        match_jnz = re.match(r"^jump_nz\s+([A-Za-z_]\w*)\s+([A-Za-z_]\w*);$", line)
        if match_jnz:
            cond_reg, target = match_jnz.groups()
            cond_hw = hardware_map[cond_reg].split(':')[1]
            asm = instruction_map["jump_nz"].format(cond=cond_hw, target=target)
            out_asm.append("\t" + asm)
            continue

        # Assignments & Math
        if "=" in line:
            left, right = map(str.strip, line[:-1].split("="))
            
            if left not in hardware_map:
                raise ValueError(f"Definition Error: '{left}' is not mapped.")
            
            dest_type, dest_hw = hardware_map[left].split(':')

            match_sub = re.match(r"@([A-Za-z_]\w*)\s*-\s*(\d+)", right)
            if match_sub:
                src_reg, val = match_sub.groups()
                src_hw = hardware_map[src_reg].split(':')[1]
                asm_template = instruction_map[f"math_sub_{dest_type}"]
                out_asm.append("\t" + asm_template.format(dest=dest_hw, src=src_hw, val=val))
                continue

            if right.isdigit():
                asm_template = instruction_map[f"assign_{dest_type}"]
                out_asm.append("\t" + asm_template.format(dest=dest_hw, val=right))
                continue

    return "\n".join(out_asm)

if __name__=="__main__":
    if len(sys.argv) != 2:
        print("Usage: python axl_compiler.py <file.axl>")
        sys.exit(1)
    print(compile_direct(sys.argv[1]))