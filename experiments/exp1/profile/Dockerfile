FROM uaib-mcp-datascibench-datasci:latest
RUN python -m pip install --no-cache-dir reportlab==4.2.5 openpyxl==3.1.5 xlrd==2.0.1 mlxtend==0.22.0 psutil==5.9.8 pypdf==5.1.0
RUN python -m pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch==2.5.1+cpu
ENV OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1 MPLBACKEND=Agg MPLCONFIGDIR=/tmp/matplotlib PYTHONHASHSEED=0 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/repo ATSUITE_RUNTIME=mcp ATSUITE_SESSION_ROOT=/out/state PROVIDER="" ATSUITE_DATASCI_TIMEOUT=600
WORKDIR /repo
ENTRYPOINT ["python", "-m", "experiments.exp1.profile.task_worker"]
