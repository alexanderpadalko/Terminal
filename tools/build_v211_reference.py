"""Generate pinned numeric Python blocks from the user's actual v2.11 source.

Development tool only. Does not emulate request.security or tick rollback.
The application never evaluates user-provided source or setting expressions.
"""
from pathlib import Path
import ast
import hashlib
import json
import re
import sys
import textwrap
from pine_expression import expression

ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path(sys.argv[1]) if len(sys.argv)>1 else ROOT/'reference/TradingView_v2.11.pine'
raw = SOURCE.read_bytes()
source = raw.decode('utf-8-sig').replace('\r\n','\n')
assert hashlib.sha256(raw).hexdigest().upper() == 'CC2E1422F6D865C7D4814B7FF7CD4EEA82F87EB89642E8EF5F158A47B85D057F'

def expression_pine(value):
    literals=[]
    def hide(m):
        literals.append(json.loads(m[0]))
        return f'_literal_{len(literals)-1}'
    value=re.sub(r'"(?:\\.|[^"\\])*"',hide,value)
    value=re.sub(r'array\.new(?:<\w+>|_\w+)\(', 'array.new(',value)
    value=re.sub(r'\b([A-Za-z_]\w*)\[(\d+)\]',lambda m:f'_past("{m[1]}", {m[2]})',value)
    # Protect generated string literals from the ternary converter.
    value=re.sub(r'"(?:\\.|[^"\\])*"',hide,value)
    value=expression(value)
    return re.sub(r'_literal_(\d+)',lambda m:repr(literals[int(m[1])]),value)


def logical_lines(block):
    result=[]
    pending=''
    depth=0
    for line in block.splitlines():
        line=re.sub(r'//.*$', '', line) if '"' not in line else line
        if not pending and (not line.strip() or line.lstrip().startswith('//')):
            continue
        pending=pending+' '+line.strip() if pending else line
        masked=re.sub(r'"(?:\\.|[^"\\])*"','""',line)
        depth+=sum(masked.count(c) for c in '([')-sum(masked.count(c) for c in ')]')
        if depth==0:
            result.append(pending)
            pending=''
    assert not pending and depth==0
    return result


class Semantics(ast.NodeTransformer):
    def visit_BinOp(self,node):
        self.generic_visit(node)
        return ast.copy_location(ast.Call(func=ast.Name(id='_binary',ctx=ast.Load()),
            args=[node.left,node.right,ast.Constant(type(node.op).__name__)],keywords=[]),node)

    def visit_Compare(self,node):
        self.generic_visit(node)
        assert len(node.ops)==1
        return ast.copy_location(ast.Call(func=ast.Name(id='_compare',ctx=ast.Load()),
            args=[node.left,node.comparators[0],ast.Constant(type(node.ops[0]).__name__)],keywords=[]),node)


def translate(block):
    lines=[]
    for line in logical_lines(block):
        content=line.lstrip()
        pad=line[:len(line)-len(content)]
        if content.startswith('else if '):
            lines.append(pad+'elif '+expression_pine(content[8:])+':')
        elif content.startswith('if '):
            lines.append(pad+'if '+expression_pine(content[3:])+':')
        elif content=='else':
            lines.append(pad+'else:')
        elif content.startswith('while '):
            lines.append(pad+'while '+expression_pine(content[6:])+':')
        elif m:=re.fullmatch(r'for (\w+) = (.+) to (.+)',content):
            lines.append(pad+f'for {m[1]} in _inclusive({expression_pine(m[2])}, {expression_pine(m[3])}):')
        elif m:=re.fullmatch(r'for (\w+) in (.+)',content):
            lines.append(pad+f'for {m[1]} in {expression_pine(m[2])}:')
        elif content in ('break','continue'):
            lines.append(pad+content)
        elif m:=re.fullmatch(r'\[([\w, ]+)\] = (.+)',content):
            lines.append(pad+'['+m[1]+'] = '+expression_pine(m[2]))
        elif m:=re.fullmatch(r'(var )?(?:\w+(?:<\w+>)?) (\w+) = (.+)',content):
            persistent,name,value=m.groups()
            if persistent:
                lines.append(pad+f'if not _has({name!r}):')
                pad+='    '
            lines.append(pad+name+' = '+expression_pine(value))
        elif m:=re.fullmatch(r'(\w+(?:\.\w+)?) (:=|\+=|-=|\*=|/=) (.+)',content):
            name,op,value=m.groups()
            lines.append(pad+name+' '+('=' if op==':=' else op)+' '+expression_pine(value))
        elif content.startswith('[') and content.endswith(']'):
            lines.append(pad+'_return = ['+expression_pine(content[1:-1])+']')
        else:
            value=expression_pine(content)
            lines.append(pad+'_return = '+value)
    tree=Semantics().visit(ast.parse('\n'.join(lines)))
    ast.fix_missing_locations(tree)
    return ast.unparse(tree)


functions={}
for m in re.finditer(r'^(f_\w+)\((.*?)\)\s*=>\s*\n',source,re.M|re.S):
    name,signature=m.groups()
    start=m.end()
    end=start
    for line in source[start:].splitlines(keepends=True):
        if line.strip() and not line.startswith(' '):
            break
        end+=len(line)
    if name.startswith('f_market') and name not in ('f_marketStepPolyline',) or name in (
        'f_mtfFlow','f_orderMovement','f_fvgUpdateActive','f_fvgAppendActive','f_updateFlowFvg',
        'f_pickSavedFlowFvg','f_updateRuleOneFvgs','f_pickRuleOneFvg','f_ruleOneWorkingZone',
        'f_mtfCopy','f_legIdm','f_ruleOneState','f_flowSnapshot','f_fvgSnapshot',
        'f_flowLiquidityEvents','f_newerCapture'):
        parameters=[part.strip().split('=')[0].split()[-1] for part in signature.split(',') if part.strip()]
        block=textwrap.dedent(source[start:end]).strip()
        functions[name]={'parameters':parameters,'code':translate(block)}

records={}
for m in re.finditer(r'^type (\w+)\s*\n((?:    .*\n|\n)+)',source,re.M):
    fields=[]
    for line in m[2].splitlines():
        if line.strip():
            parts=line.strip().split(' = ',1)
            fields.append((parts[0].split()[-1],None if len(parts)==1 else expression_pine(parts[1])))
    records[m[1]]=fields

sections={
 'market_init':source.split('// Актуальное состояние после полного ретроспективного прохода.',1)[1].split('// Первичная инициализация',1)[0],
 'market_replay':source.split('if marketNeedsReplay\n',1)[1].split('// Предварительное состояние обновляем отдельно',1)[0],
 'market_live':source.split('if enableMarketStructure and (marketNeedsReplay or barstate.islast)\n',1)[1].split('//------------------------------------------------------------------------------\n// Отображение только актуального',1)[0],
}
sections={name:translate(textwrap.dedent(block).strip()) for name,block in sections.items()}
payload={'sha256':hashlib.sha256(raw).hexdigest().upper(),'version':'2.11 H1 + H4',
         'functions':functions,'records':records,'sections':sections}
destination=ROOT/'flowdesk/data/reference_v211.json'
destination.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'functions':len(functions),'sections':len(sections),'bytes':destination.stat().st_size}))
