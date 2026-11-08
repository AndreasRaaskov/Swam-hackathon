# Jourapist interview pipeline. Keys are NOT baked in: pass them at runtime (--env-file .env).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    JOURAPIST_DATA=/data \
    JOURAPIST_OUTPUTS=/outputs \
    JOURAPIST_CONFIG=/config \
    MPLCONFIGDIR=/tmp/matplotlib

WORKDIR /app
# Dependencies in small cached layers: Docker Desktop on a small VM can drop long builds,
# and a retry then resumes from the last finished layer. Versions mirror pyproject.toml;
# `pip check` below fails the build if they drift apart.
RUN pip install --no-cache-dir "openai==3.46.0" "anthropic==2.6.0" "pydantic==2.14.3" \
        "pyyaml==6.0.3" "python-dotenv>=1.0" "tabulate>=0.9" "pytest>=8"
RUN pip install --no-cache-dir "numpy>=2.0"
RUN pip install --no-cache-dir "pandas==3.1.1"
RUN pip install --no-cache-dir "scipy==1.18.1"
RUN pip install --no-cache-dir "scikit-learn==1.9.1"
RUN pip install --no-cache-dir "matplotlib==3.11.2"
RUN pip install --no-cache-dir "adaption==0.19.0"
# MCP audit service (`jourapist serve`); Railway runs it with start command `python -m jourapist serve`
RUN pip install --no-cache-dir "mcp==2.3.0" "uvicorn==0.54.0"
COPY pyproject.toml ./
COPY jourapist ./jourapist
RUN pip install --no-cache-dir --no-deps . && pip check
COPY tests ./tests
# Default config ships in the image; mounting a host dir on /config overrides it.
COPY config /config

RUN useradd --create-home --uid 1000 app \
 && mkdir -p /data /outputs \
 && chown -R app:app /data /outputs /config
USER app

ENTRYPOINT ["python", "-m", "jourapist"]
CMD ["--help"]
