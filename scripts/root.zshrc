# Installed at /root/.zshrc.
#
# The entrypoint runs as root because nordvpnd needs NET_ADMIN, which means a
# bare `docker compose exec downloadinux zsh` also lands here. That's the wrong
# place to download from: nothing is configured, and anything you fetch ends up
# root-owned on the host. Say so loudly rather than letting it happen quietly.

print -P "%F{yellow}[downloadinux]%f This is the root shell — no aliases, no configs, and downloads land root-owned."
# Not $USERNAME — zsh clobbers that with the current user, which is root here.
print -P "%F{yellow}[downloadinux]%f You want:  %F{cyan}just shell%f   (or: docker compose exec -u ${DL_USER:-dl} downloadinux zsh)"
print -P "%F{8}Root is only useful for nordvpnd troubleshooting: /var/log/nordvpnd.log%f"
