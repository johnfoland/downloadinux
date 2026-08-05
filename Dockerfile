# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# downloadinux — Debian 13 "Trixie"
#
# Trixie is the current Debian stable: modern toolchain (Python 3.13, ffmpeg 7)
# with Debian's boring-on-purpose stability. Headless only, zsh as the shell.
# ---------------------------------------------------------------------------
FROM debian:trixie-slim

ARG PUID=1000
ARG PGID=1000
ARG USERNAME=dl

# DL_USER duplicates USERNAME because zsh overwrites $USERNAME with the
# *current* user — so zsh startup files must read DL_USER instead.
ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    TERM=xterm-256color \
    USERNAME=${USERNAME} \
    DL_USER=${USERNAME}

# --- Base system + media tooling -------------------------------------------
# atomicparsley  -> yt-dlp --embed-thumbnail for mp4/m4a
# libimage-exiftool-perl -> the `exifmod` function
# openvpn/iptables/iproute2 -> required by the NordVPN daemon
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl wget gnupg gosu \
        zsh git tmux less nano jq unzip \
        procps iproute2 iputils-ping dnsutils \
        ffmpeg atomicparsley libimage-exiftool-perl \
        python3 python3-venv \
        openvpn iptables nftables \
        fzf ripgrep bat tzdata \
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
# Match the host UID/GID so files landing in the shared ~/Downloads mount
# come back owned by you. GID may already exist in Debian (e.g. 20 = dialout).
RUN if ! getent group "${PGID}" >/dev/null; then groupadd -g "${PGID}" "${USERNAME}"; fi \
    && useradd -m -u "${PUID}" -g "${PGID}" -s /usr/bin/zsh "${USERNAME}" \
    && usermod -aG nordvpn "${USERNAME}" \
    && mkdir -p "/home/${USERNAME}/Downloads" "/home/${USERNAME}/cookies" \
                "/home/${USERNAME}/.config/yt-dlp" "/home/${USERNAME}/.config/gallery-dl" \
    && chown -R "${PUID}:${PGID}" "/home/${USERNAME}"

# --- zsh / oh-my-zsh --------------------------------------------------------
USER ${USERNAME}
WORKDIR /home/${USERNAME}
ENV ZSH=/home/${USERNAME}/.oh-my-zsh
RUN sh -c "$(curl -fsSL https://raw.githubusercontent.com/ohmyzsh/ohmyzsh/master/tools/install.sh)" "" --unattended \
    && git clone --depth=1 https://github.com/zsh-users/zsh-autosuggestions \
         "${ZSH}/custom/plugins/zsh-autosuggestions" \
    && git clone --depth=1 https://github.com/zsh-users/zsh-syntax-highlighting \
         "${ZSH}/custom/plugins/zsh-syntax-highlighting" \
    && printf '%s\n' 'source "$HOME/.zsh-custom/zshrc"' > "/home/${USERNAME}/.zshrc"

COPY --chown=${PUID}:${PGID} zsh/            /home/${USERNAME}/.zsh-custom/
COPY --chown=${PUID}:${PGID} config/yt-dlp/     /home/${USERNAME}/.config/yt-dlp/
COPY --chown=${PUID}:${PGID} config/gallery-dl/ /home/${USERNAME}/.config/gallery-dl/

USER root
COPY scripts/entrypoint.sh /usr/local/bin/entrypoint.sh
COPY scripts/vpn.sh        /usr/local/bin/vpn
# `exec` without -u lands as root; make that obvious instead of silently wrong.
COPY scripts/root.zshrc    /root/.zshrc
RUN chmod +x /usr/local/bin/entrypoint.sh /usr/local/bin/vpn

WORKDIR /home/${USERNAME}/Downloads
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["sleep", "infinity"]
