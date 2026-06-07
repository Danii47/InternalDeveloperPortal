# ─────────────────────────────────────────────────────────────────────────────
# Proxmox IDP — Backend (FastAPI + Terraform)
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.13-slim

# ── System packages ───────────────────────────────────────────────────────────
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      wget \
      unzip \
      ca-certificates \
      curl \
      gosu \
 && rm -rf /var/lib/apt/lists/*

# Usuario no-root para ejecutar la aplicación (el entrypoint baja privilegios con gosu).
RUN groupadd -g 10001 idp && useradd -u 10001 -g idp -m -s /usr/sbin/nologin idp

# ── Terraform CLI ─────────────────────────────────────────────────────────────
# Override at build time: --build-arg TERRAFORM_VERSION=x.y.z
ARG TERRAFORM_VERSION=1.15.5

RUN set -eux; \
    ARCH="$(uname -m)"; \
    case "$ARCH" in \
        x86_64)  TF_ARCH="amd64" ;; \
        aarch64) TF_ARCH="arm64" ;; \
        armv7l)  TF_ARCH="arm"   ;; \
        *)       echo "Unsupported arch: $ARCH" && exit 1 ;; \
    esac; \
    wget -q \
      "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_linux_${TF_ARCH}.zip" \
      -O /tmp/terraform.zip; \
    unzip /tmp/terraform.zip -d /usr/local/bin/; \
    rm /tmp/terraform.zip; \
    terraform version

# ── Python application ────────────────────────────────────────────────────────
WORKDIR /app

# Install dependencies first (better layer cache reuse)
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
# (terraform/ and .env are excluded via .dockerignore)
COPY . .

# Ensure the entrypoint is executable inside the image
RUN chmod +x docker-entrypoint.sh

# ── Runtime ───────────────────────────────────────────────────────────────────
EXPOSE 8000

ENTRYPOINT ["./docker-entrypoint.sh"]
