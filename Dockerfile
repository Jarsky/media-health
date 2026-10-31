FROM python:3.13-slim
# PUID/PGID: the user scans run as; it needs read access to the media
ARG PUID=1000
ARG PGID=1000
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg sqlite3 \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd -o -g "$PGID" media \
 && useradd -o -u "$PUID" -g "$PGID" -m -s /bin/bash mh
WORKDIR /src
COPY pyproject.toml README.md media_health.py ./
RUN pip install --no-cache-dir . && rm -rf /src
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
COPY bashrc.sh /tmp/bashrc.sh
RUN chmod 755 /usr/local/bin/entrypoint.sh \
 && media-health --completion > /etc/media-health-completion.bash \
 && cat /tmp/bashrc.sh >> /etc/bash.bashrc \
 && grep '^PS1=' /tmp/bashrc.sh >> /home/mh/.bashrc && rm /tmp/bashrc.sh
# NVDEC (--gpu) when run with the NVIDIA runtime
ENV NVIDIA_DRIVER_CAPABILITIES=compute,video,utility
USER mh
WORKDIR /media
ENTRYPOINT ["entrypoint.sh"]
CMD ["serve"]
