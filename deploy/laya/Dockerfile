FROM python:3.12-slim-bookworm AS contracts
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir '.[serve,local]'
COPY tests/test_laya_hosted.py ./tests/test_laya_hosted.py
RUN python -m unittest discover -s tests -p test_laya_hosted.py -v

FROM contracts AS runtime
ARG TORCH_INDEX=cu128
ARG TORCH_VERSION=2.11.0
# Exact SDK and checkpoint revisions are separate: both must be reviewed before changing.
ARG LAYA_SDK_REVISION=9d955671415fc19f069b9cc998928075c1f255ec
RUN pip install --no-cache-dir "torch==${TORCH_VERSION}" --index-url "https://download.pytorch.org/whl/${TORCH_INDEX}" \
    && pip install --no-cache-dir "https://github.com/NandhaKishorM/laya/archive/${LAYA_SDK_REVISION}.tar.gz" \
    && pip check
RUN groupadd --gid 10001 laya && useradd --uid 10001 --gid laya --create-home laya \
    && mkdir -p /workspace/huggingface && chown -R laya:laya /workspace
ENV LAYA_DEVICE=cuda \
    LAYA_MODEL_REVISION=55cf4c4ebb4ebe31b2550e8bdf3bd21b99753851 \
    LAYA_CLIENTS_FILE=/run/laya/clients.json \
    LAYA_MAX_INFLIGHT=4 \
    LAYA_REQUESTS_PER_MINUTE=600 \
    LAYA_DEADLINE_SECONDS=10 \
    HF_HOME=/workspace/huggingface \
    OMP_NUM_THREADS=4 \
    TOKENIZERS_PARALLELISM=false \
    TORCH_DISABLE_NATIVE_JIT=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8090
USER laya
EXPOSE 8090
HEALTHCHECK --start-period=300s --interval=30s --timeout=5s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/health', timeout=4)"
CMD ["python", "-m", "amplifier_fast_decisions.laya_hosted"]
