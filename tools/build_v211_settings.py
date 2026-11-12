"""Copy every current-source input into the application settings schema."""
from pathlib import Path
import ast
import hashlib
import json
import re
import sys
from pine_inputs import extract

root=Path(__file__).resolve().parents[1]
source=Path(sys.argv[1]) if len(sys.argv)>1 else root/'reference/TradingView_v2.11.pine'
digest=hashlib.sha256(source.read_bytes()).hexdigest().upper()
assert digest=='CC2E1422F6D865C7D4814B7FF7CD4EEA82F87EB89642E8EF5F158A47B85D057F'
items=extract(source)
inputs=[]
for row in items:
    item={k:row[k] for k in ('variable','type','title','group','line')}
    value=row['default']
    if row['type']=='color':
        rgb=re.search(r'#[0-9A-Fa-f]{6}',value)[0].upper()
        transparency=int(re.search(r',\s*(\d+)\)',value)[1]) if value.startswith('color.new') else 0
        item['default']=rgb+f'{round(255*(100-transparency)/100):02X}'
    elif row['type']=='bool':item['default']=value=='true'
    elif row['type'] in ('int','float'):
        item['default']=ast.literal_eval(value)
        for old,new in [('minval','min'),('maxval','max'),('step','step')]:
            if old in row['limits']:item[new]=ast.literal_eval(row['limits'][old])
    elif row['type']=='source':
        item['default']=value
        item['options']=['open','high','low','close','hl2','hlc3','ohlc4','volume']
    else:
        item['default']=value.removeprefix('size.') if value.startswith('size.') else ast.literal_eval(value)
        item['options']=ast.literal_eval(re.sub(r'size\.(\w+)',r'"\1"',row['limits']['options']))
    item['tooltip']=ast.literal_eval(row['arguments'].get('tooltip','""'))
    inputs.append(item)
payload={'sha256':'CC2E1422F6D865C7D4814B7FF7CD4EEA82F87EB89642E8EF5F158A47B85D057F','inputs':inputs}
(root/'flowdesk/data/settings_v211.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
print('Settings:',len(inputs))
