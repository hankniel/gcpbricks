# The console (gcpbricks.console): its API server, serving the web app CI builds first
# into console/web/dist (the console-web job; locally, `npm run build` in console/web).
# Deployed by terraform/modules/console.
FROM python:3.11-slim
COPY pyproject.toml README.md /src/
COPY src /src/src
# The wheel force-includes the bundle module (pyproject.toml).
COPY terraform/modules/bundle /src/terraform/modules/bundle
RUN pip install --no-cache-dir "/src[console]"
COPY console/web/dist /app/static
ENV MLOPS_CONSOLE_STATIC=/app/static
CMD exec uvicorn gcpbricks.console.app:app --host 0.0.0.0 --port ${PORT:-8080}
