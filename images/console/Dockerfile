# The console (gcpbricks.console): its API server, serving the web app CI builds first
# into console/web/dist (the console-web job; locally, `npm run build` in console/web).
# Deployed by terraform/modules/console.
FROM python:3.11-slim
# Console-created quality monitors deploy their bundle (console.monitor_deploy): git lists its
# files, terraform applies it, at CI's version.
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends git curl unzip ca-certificates > /dev/null \
    && curl -sSfo /tmp/tf.zip "https://releases.hashicorp.com/terraform/1.16.3/terraform_1.16.3_linux_amd64.zip" \
    && unzip -qo /tmp/tf.zip -d /usr/local/bin terraform && rm /tmp/tf.zip \
    && apt-get purge -y -qq curl unzip > /dev/null && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml README.md /src/
COPY src /src/src
# The wheel force-includes the bundle module (pyproject.toml).
COPY terraform/modules/bundle /src/terraform/modules/bundle
RUN pip install --no-cache-dir "/src[console]"
COPY console/web/dist /app/static
ENV MLOPS_CONSOLE_STATIC=/app/static
CMD exec uvicorn gcpbricks.console.app:app --host 0.0.0.0 --port ${PORT:-8080}
