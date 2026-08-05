# downloadinux aliases
# Ported from ~/.oh-my-zsh/custom/aliases.zsh — download-related entries only.
# Host-specific ones (brew, chezmoi, pbcopy, dscacheutil) are Mac-only and
# were left behind.

# Override `more` with `bat`
alias more="batcat"
alias bat="batcat"

# Download best MP4 quality with yt-dlp
alias dlmp4="yt-dlp --config-location ~/.config/yt-dlp/dlmp4.conf"

# Download image gallery with gallery-dl
alias dlgal="gallery-dl"

# Print directories in current $PATH
alias chemin='echo -e ${PATH//:/\\n}'

# --- Container conveniences -------------------------------------------------

# VPN status at a glance
alias vpns="nordvpn status"

# Where am I really coming from?
alias myip="curl -s https://ipinfo.io/json | jq -r '.ip + \"  \" + .city + \", \" + .country + \"  (\" + .org + \")\"'"

# Update the downloaders in place (until the next image rebuild)
alias dlupdate="pip install --upgrade 'yt-dlp[default,curl-cffi]' gallery-dl"
