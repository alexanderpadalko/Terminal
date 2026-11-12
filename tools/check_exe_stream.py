"""Real Windows EXE must resolve DNS and receive automatic ISS+ M1 updates."""
import json
import os
from pathlib import Path
import subprocess
import sys


def main():
    exe=Path(sys.argv[1]).resolve()
    symbol,board=sys.argv[2:4] if len(sys.argv)>=4 else ('SBER','TQBR')
    output=Path('issplus-exe-result.json' if board=='TQBR' else 'issplus-exe-rfud-result.json').resolve()
    startup=None
    if os.name=='nt':
        startup=subprocess.STARTUPINFO()
        startup.dwFlags|=subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow=0
    completed=subprocess.run([str(exe),'--diagnose-stream',str(output),symbol,board],startupinfo=startup,timeout=150)
    assert output.exists(),'Packaged stream diagnostic did not produce a result'
    result=json.loads(output.read_text(encoding='utf-8'))
    print('PACKAGED_STREAM',json.dumps(result,ensure_ascii=False),flush=True)
    assert result['frozen'],'The check must execute the bundled application'
    assert completed.returncode==0 and result['passed'],result.get('error','Packaged stream failed')
    assert result['executor_after']=='ok' and result['dns_addresses']>0
    assert result['automatic_updates'] and result['counts']['M1']>=2
    assert result['http_market_requests']==0


if __name__=='__main__':
    main()
