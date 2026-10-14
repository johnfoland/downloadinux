# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# downloadinux — Debian 13 "Trixie"
#
# Trixie is the current Debian stable: modern toolchain (Python 3.13, ffmpeg 7)
# with Debian's boring-on-purpose stability. Headless only, zsh as the shell.
# ---------------------------------------------------------------------------
FROM debian:trixie-slim

# zsh overwrites USERNAME; startup files use DL_USER instead.
ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    TERM=xterm-256color \
    DL_USER=dl

# --- Base system + media tooling -------------------------------------------
# atomicparsley  -> yt-dlp --embed-thumbnail for mp4/m4a
# libimage-exiftool-perl -> the `exifmod` function
# openvpn/iptables/iproute2 -> required by the NordVPN daemon
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl wget gnupg gosu \
        zsh git less nano jq unzip \
        procps iproute2 iputils-ping dnsutils \
        ffmpeg atomicparsley libimage-exiftool-perl \
        python3 python3-venv \
        openvpn iptables nftables \
        fzf ripgrep bat tzdata \
    && rm -rf /var/lib/apt/lists/*

# --- Node.js ------------------------------------------------------------
# JS runtime for yt-dlp's built-in EJS n-challenge solver. Without one,
# YouTube's nsig-protected formats silently vanish and web_safari gets
# SABR-forced with no direct URLs, leaving only thumbnails to "download".
# Debian trixie's own `nodejs` package is stuck on v20; yt-dlp's runtime
# detector requires >=22, so pull current LTS from NodeSource instead.
# Done at build time (not via a runtime install) so it isn't at the mercy
# of the NordVPN tunnel the container routes through once it's up.
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# --- NordVPN ----------------------------------------------------------------
# Nord ships one "stable" suite for every Debian/Ubuntu release.
RUN curl -fsSL https://repo.nordvpn.com/gpg/nordvpn_public.asc \
        -o /usr/share/keyrings/nordvpn.asc \
    && echo "deb [signed-by=/usr/share/keyrings/nordvpn.asc] https://repo.nordvpn.com/deb/nordvpn/debian stable main" \
        > /etc/apt/sources.list.d/nordvpn.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends nordvpn \
    && rm -rf /var/lib/apt/lists/*

# --- Downloaders ------------------------------------------------------------
# Debian's yt-dlp/gallery-dl packages go stale fast, and Trixie enforces
# PEP 668, so everything lives in one venv that owns the front of $PATH.
ENV VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH
RUN python3 -m venv "$VIRTUAL_ENV" \
    && pip install --no-cache-dir --upgrade pip wheel \
    && pip install --no-cache-dir \
        "yt-dlp[default,curl-cffi]" \
        gallery-dl \
        rich-cli

# --- User -------------------------------------------------------------------
# Host identity is applied at start, never baked into the image (DEC-9).
RUN groupadd -g 1000 dl \
    && useradd -m -u 1000 -g 1000 -s /usr/bin/zsh dl \
    && usermod -aG nordvpn dl

# --- zsh / oh-my-zsh --------------------------------------------------------
USER dl
WORKDIR /home/dl
ENV ZSH=/home/dl/.oh-my-zsh
RUN sh -c "$(curl -fsSL https://raw.githubusercontent.com/ohmyzsh/ohmyzsh/master/tools/install.sh)" "" --unattended \
    && git clone --depth=1 https://github.com/zsh-users/zsh-autosuggestions \
         "${ZSH}/custom/plugins/zsh-autosuggestions" \
    && git clone --depth=1 https://github.com/zsh-users/zsh-syntax-highlighting \
         "${ZSH}/custom/plugins/zsh-syntax-highlighting" \
    && printf '%s\n' 'source "$HOME/.zsh-custom/zshrc"' > /home/dl/.zshrc

COPY --chown=1000:1000 zsh/ /home/dl/.zsh-custom/

USER root
# --- Cookie runner (Named cookie sources §3, "Packaging"; DEC-36) -------------
# --- and worker (Persistent worker and jobs §1, "Packaging"; DEC-41) --------
# The package, root-owned and read-only to dl, for the system interpreter
# only: the runner and the worker import nothing from /opt/venv.
COPY src/downloadinux/ /opt/downloadinux/downloadinux/
COPY scripts/downloadinux-runner /usr/local/bin/downloadinux-runner
COPY scripts/downloadinux-worker /usr/local/bin/downloadinux-worker
RUN chmod -R go-w /opt/downloadinux \
    && chmod 755 /usr/local/bin/downloadinux-runner /usr/local/bin/downloadinux-worker \
    && /usr/bin/python3 -m compileall -q /opt/downloadinux

COPY scripts/entrypoint.sh /usr/local/bin/entrypoint.sh
COPY scripts/vpn.sh        /usr/local/bin/vpn
# `exec` without -u lands as root; make that obvious instead of silently wrong.
COPY scripts/root.zshrc    /root/.zshrc
RUN chmod +x /usr/local/bin/entrypoint.sh /usr/local/bin/vpn

WORKDIR /downloads
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
# The worker is the main process, as dl through the entrypoint's gosu, under
# Compose's init (Persistent worker and jobs §1, "Packaging").
CMD ["downloadinux-worker", "serve", "--cookies", "/cookies", "--state", "/state"]
