FROM wabber-phase1-reference:20261025-cpu-lstm
USER root
RUN mv /app/.python_user /opt/python-user && rm -rf /app /root/.aws /root/.config /root/.cache /root/.huggingface /home/*
COPY AgentTools/sandbox_worker.py AgentTools/result_checks.py AgentTools/extended_checks.py AgentTools/resident_kernel.py DataSci/kernel.py /opt/wabber/
ENV HOME=/tmp OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1 MPLCONFIGDIR=/tmp/matplotlib PYTHONHASHSEED=0 PYTHONUSERBASE=/opt/python-user PYTHONPATH=/opt/python-user/lib/python3.11/site-packages
WORKDIR /work
USER 65532:65532
ENTRYPOINT ["python", "/opt/wabber/sandbox_worker.py"]
