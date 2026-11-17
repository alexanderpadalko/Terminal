"""Ship a numeric/code cache revision to frozen builds without Python sources."""
from pathlib import Path
import json
import sys

root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root))
from flowdesk.calculation_store import model_version,SCHEMA,MODEL_FILES

target=root/'flowdesk/data/calculation_model.json'
target.write_text(json.dumps({'schema':SCHEMA,'sha256':model_version(),'files':MODEL_FILES},indent=2)+'\n',encoding='utf-8')
print('CALCULATION_MODEL_OK '+model_version())
