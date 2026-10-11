FROM python:3.14-slim AS base

# Pinned rather than left to whatever --system would pick on its own: a
# bind-mounted host directory (for storage.data_dir) needs a stable,
# documented UID/GID to be pre-chowned to (`chown -R 999:999`).
RUN groupadd --gid 999 bridge \
    && useradd --system --uid 999 --gid 999 --create-home --shell /usr/sbin/nologin bridge

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bridge/ ./bridge/
COPY main.py .

# storage.data_dir defaults to the relative "./data" (config.example.yaml),
# which resolves against WORKDIR /app -- owned by root, not the "bridge"
# user below. The mounted config.yaml must set storage.data_dir to /data.
RUN mkdir -p /data && chown bridge:bridge /data
VOLUME ["/data"]

USER bridge
ENV BRIDGE_CONFIG=/config/config.yaml

# ENTRYPOINT is exec-form, so an argument passed to `docker run` is APPENDED
# to it, not substituted for it. To run a one-off command in this image
# (generating the AppService registration, say) override the entrypoint:
#   docker run --rm --entrypoint python -v ...:/config/config.yaml:ro \
#     -v bridge-data:/data <image> -m bridge.appservice \
#     /config/config.yaml /data/appservice-registration.yaml
# /data is the only writable path in this image -- /config is a read-only
# single-file bind mount -- so that's where the registration file must land.

# bridge.listen_host defaults to 127.0.0.1 (config.example.yaml). The
# mounted config.yaml MUST override this to 0.0.0.0, or nothing outside the
# container's own network namespace can reach it even with the port
# published. EXPOSE is documentation only and assumes the default
# listen_port.
EXPOSE 8091

ENTRYPOINT ["python", "main.py"]
