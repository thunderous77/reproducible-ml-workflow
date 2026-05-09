# Runtime image — environment only, no application code.
#
# Layered with the wheel:
#   • this image  →  Python interpreter + heavy dependencies (rebuilt on dep changes)
#   • the wheel   →  application code with git SHA baked in    (rebuilt on every commit)
#
# At run time, the entry script `pip install --no-deps`s the wheel into this
# image's Python — adding the app code without touching the runtime.

FROM python:3.11-slim

# Bring in just the dep declaration. The wheel itself ships the application
# code; this image's job is the runtime environment.
COPY pyproject.toml /tmp/pyproject.toml

# Install runtime dependencies declared in pyproject.toml.
#
# In a real ML project this is where torch / cuda / transformers etc go.
# This demo's wheel has no deps so the install is a no-op — but the
# infrastructure is here, so adding deps is just an edit to pyproject.toml.
RUN python -c "import tomllib, sys; print('\n'.join(tomllib.loads(open('/tmp/pyproject.toml').read())['project'].get('dependencies', []) or []))" > /tmp/deps.txt \
 && if [ -s /tmp/deps.txt ]; then \
        pip install --no-cache-dir -r /tmp/deps.txt; \
    fi \
 && rm -rf /root/.cache /tmp/deps.txt /tmp/pyproject.toml

WORKDIR /work

# Default command is overridden at run time by run.sh.
CMD ["python", "-c", "print('mypkg-runtime image — override CMD with your entry point.')"]
