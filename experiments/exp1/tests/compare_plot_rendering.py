"""Compare fixed-program renderings in the offline Tool container, never during E1-A."""
import io,json,tarfile
from pathlib import Path
import numpy as np
from PIL import Image

reference=Path('/diag/csv34-architecture-x86-v1/state/E1aAliFc/architecture-calibration/workspace/v2/artifacts/lorenz_curve.png')
with tarfile.open('/diag/csv34-architecture-arm-v1/state/branch_artifacts/architecture-calibration/i0-lorenz.tar') as archive:
    raw=archive.extractfile('lorenz_curve.png').read()
a=np.asarray(Image.open(reference).convert('RGBA')).astype(float)
b=np.asarray(Image.open(io.BytesIO(raw)).convert('RGBA')).astype(float)
assert a.shape==b.shape
result={'shape':list(a.shape),'differing_pixels':int(np.any(a!=b,axis=2).sum()),'differing_pixel_fraction':float(np.any(a!=b,axis=2).mean()),'max_channel_difference':float(np.abs(a-b).max()),'channel_rmse':float(np.sqrt(np.mean((a-b)**2)))}
print(json.dumps(result,indent=2))
Image.open(io.BytesIO(raw)).save('/diag/csv34-arm-lorenz.png')
Image.open(reference).save('/diag/csv34-x86-lorenz.png')
