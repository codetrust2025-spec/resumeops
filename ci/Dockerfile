# Tools for the ResumeOps checks, preinstalled so a routine CI run never waits on Ubuntu mirrors.
# CI builds this image only when this file or requirements.txt changes: the image tag is a hash of both.
# Ubuntu 24.04 matches the GitHub runner the checks used before, so TeX Live and poppler are the same versions.
FROM ubuntu:24.04
LABEL org.opencontainers.image.source="https://github.com/codetrust2025-spec/resumeops" \
      org.opencontainers.image.description="ResumeOps CI tools: TeX Live, poppler, Python 3.12, Node.js, Chromium"
ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright PATH=/opt/venv/bin:$PATH
# Bounded apt attempts. If the default archive or security mirror stalls, retry once on Azure's Ubuntu mirror,
# which carries both (a security-mirror outage is what blocked CI on 2026-11-19).
RUN set -eux; \
    apt_all() { \
      timeout 600 apt-get -o Acquire::Retries=3 -o Acquire::http::Timeout=30 update && \
      timeout 900 apt-get -o Acquire::Retries=3 -o Acquire::http::Timeout=30 install -y --no-install-recommends \
        texlive-latex-extra lmodern poppler-utils python3 python3-venv nodejs git ca-certificates fonts-dejavu-core; \
    }; \
    apt_all || { \
      sed -i -E 's#https?://(archive|security)\.ubuntu\.com#http://azure.archive.ubuntu.com#g' /etc/apt/sources.list.d/ubuntu.sources; \
      apt_all; \
    }; \
    rm -rf /var/lib/apt/lists/*
COPY requirements.txt /tmp/requirements.txt
RUN set -eux; \
    python3 -m venv /opt/venv; \
    pip install -r /tmp/requirements.txt; \
    python -m playwright install --with-deps chromium; \
    rm -rf /var/lib/apt/lists/* /tmp/requirements.txt; \
    pdflatex --version | head -1; pdftotext -v 2>&1 | head -1; node --version; python --version
