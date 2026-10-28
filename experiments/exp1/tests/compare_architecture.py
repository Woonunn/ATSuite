"""Offline numerical diagnostic, run inside the trusted Docker Tool environment."""
import io,json,pickle,tarfile
from pathlib import Path
import numpy as np
import pandas as pd
from joblib.externals import cloudpickle

reference=Path('/diag/architecture-x86-v1/state/E1aAliFc/architecture-calibration/workspace/v3/artifacts')
with tarfile.open('/diag/architecture-arm-v4/state/branch_artifacts/architecture-calibration/i0-model.tar') as archive:
    actual=pd.read_csv(archive.extractfile('model_predictions.csv'))
    model=pickle.load(archive.extractfile('model_checkpoint.pkl'))['model']
    state=cloudpickle.load(archive.extractfile('state.pkl'))
expected=pd.read_csv(reference/'model_predictions.csv')
assert list(actual.columns)==list(expected.columns) and actual.shape==expected.shape
result={'rows':len(actual),'columns':{},'arm_state':state['metrics'],'state_names':sorted(state),'arm_feature_importances':state['feature_importances']}
for name in actual:
    a=actual[name];b=expected[name]
    result['columns'][name]={'differing':int((a!=b).sum()),'max_abs_difference':float(np.abs(a-b).max()) if pd.api.types.is_numeric_dtype(a) else None}
ref_model=pickle.load(open(reference/'model_checkpoint.pkl','rb'))['model']
result['fitted_feature_importance_max_difference']=float(np.abs(ref_model.feature_importances_-model.feature_importances_).max())
print(json.dumps(result,indent=2,default=lambda x:x.tolist() if hasattr(x,'tolist') else str(x)))
