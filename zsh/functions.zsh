# downloadinux functions
# Ported from ~/.oh-my-zsh/custom/functions.zsh. The downloaders run the
# presets' argument lists, the ones `dlx dl` and `dlx batch` build
# (docs/target-design.md, Presets; DEC-20), with the user-agent from
# DL_USER_AGENT, which is fixed when the container is created.

# Each preset's argument list, without URLs, into $reply. Extra arguments go
# right after the program (`dlgal_batch`'s -q).
_preset_video() {
  reply=(yt-dlp "$@" --config-locations /config/video/yt-dlp.conf
    --paths /downloads/dlmp4 --cookies /cookies/cookies.txt
    --add-headers "User-Agent:${DL_USER_AGENT}")
}

_preset_gallery() {
  reply=(gallery-dl "$@" --config /config/gallery/config.json
    -o extractor.archive=/downloads/dlgal/gallery-dl-archive.sqlite3
    -o "extractor.user-agent=${DL_USER_AGENT}"
    -o "extractor.instagram.user-agent=${DL_USER_AGENT}"
    -o extractor.instagram.base-directory=/downloads/dlgal
    -o extractor.instagram.cookies=/cookies/cookies.txt)
}

# Download best MP4 quality with yt-dlp
dlmp4() {
  local reply
  _preset_video
  "${reply[@]}" "$@"
}

# Download image gallery with gallery-dl
dlgal() {
  local reply
  _preset_gallery
  "${reply[@]}" "$@"
}

# -------------------------------------------------------------------------------------------------

# Batch download videos from a file using yt-dlp
# Usage: dlmp4_batch <input_file>
dlmp4_batch() {
  # 1. Graceful handling: Check if a filename was provided
  if [[ -z "$1" ]] || [[ ! -f "$1" ]]; then
    rich --print "[bold red]Error:[/bold red] Valid input file required." --panel rounded
    rich --print "Usage: [bold cyan]dlmp4_batch[/bold cyan] <input_file>"
    return 1
  fi

  local reply
  _preset_video
  _batch_downloader_core "$1" "dlmp4" "Batch Download" "blue" "${reply[@]}"
}

# -------------------------------------------------------------------------------------------------

# Batch download image galleries from a file using gallery-dl
# Usage: dlgal_batch <input_file>
dlgal_batch() {
  # 1. Graceful handling: Check if a filename was provided
  if [[ -z "$1" ]] || [[ ! -f "$1" ]]; then
    rich --print "[bold red]Error:[/bold red] Valid input file required." --panel rounded
    rich --print "Usage: [bold cyan]dlgal_batch[/bold cyan] <input_file>"
    return 1
  fi

  local reply
  _preset_gallery -q
  _batch_downloader_core "$1" "dlgal" "Gallery Batch Download" "magenta" "${reply[@]}"
}

# -------------------------------------------------------------------------------------------------

# Interactively modify all relevant dates using exiftool
# Usage: exifmod <input_file_or_directory>
exifmod() {
  # 1. Graceful handling
  if [[ -z "$1" ]] || [[ ! -e "$1" ]]; then
    rich --print "[bold red]Error:[/bold red] Valid input file or directory required." --panel rounded
    rich --print "Usage: [bold cyan]exifmod[/bold cyan] <input_file_or_directory>"
    return 1
  fi

  local target_path="$1"

  rich --print "[bold blue]ExifTool Master Date Modifier[/bold blue]
Target: [b]${target_path}[/b]" --panel rounded --style blue

  echo ""
  rich --print "[bold cyan]--- Target Date & Time ---[/bold cyan]"

  # 2. Grab current date/time for defaults
  local cur_year=$(date +%Y)
  local cur_month=$(date +%m)
  local cur_day=$(date +%d)
  local cur_hour=$(date +%H)
  local cur_minute=$(date +%M)
  local cur_second=$(date +%S)

  # 3. Prompt for inputs (showing defaults)
  local c_year c_month c_day c_hour c_minute c_second
  read "c_year?  Year   (YYYY) [${cur_year}]: "
  read "c_month?  Month  (MM)   [${cur_month}]: "
  read "c_day?  Day    (DD)   [${cur_day}]: "
  read "c_hour?  Hour   (HH)   [${cur_hour}]: "
  read "c_minute?  Minute (MM)   [${cur_minute}]: "
  read "c_second?  Second (SS)   [${cur_second}]: "

  # Zero-pad inputs and apply current date fallbacks if empty
  local target_date=$(printf "%04d:%02d:%02d %02d:%02d:%02d" \
    "${c_year:-$cur_year}" \
    "${c_month:-$cur_month}" \
    "${c_day:-$cur_day}" \
    "${c_hour:-$cur_hour}" \
    "${c_minute:-$cur_minute}" \
    "${c_second:-$cur_second}")

  echo ""
  rich --print "All dates will be forced to: [bold green]${target_date}[/bold green]"
  echo ""

  # 4. Confirmation prompt
  local proceed_ans
  rich --print "[bold yellow]Proceed with modifying dates?[/bold yellow] (Y/n): " | tr -d '\n'
  read "proceed_ans"

  if [[ "$proceed_ans" =~ ^[Nn] ]]; then
    echo ""
    rich --print "[dim]Operation cancelled by user.[/dim]"
    return 0
  fi

  echo ""
  # 5. Show BEFORE state
  rich --print "[bold magenta]--- Before ExifTool Modifications ---[/bold magenta]"
  exiftool -time:all -G1 -a "$target_path"
  echo ""

  rich --print "[dim]Executing ExifTool...[/dim]"

  # 6. Run ExifTool
  if exiftool -r -overwrite_original -P \
      -AllDates="${target_date}" \
      -FileModifyDate="${target_date}" \
      -FileCreateDate="${target_date}" \
      "$target_path"; then
    echo ""
    rich --print "[bold green]✓ Exif metadata updated successfully![/bold green]"
  else
    echo ""
    rich --print "[bold red]✗ ExifTool encountered an error.[/bold red]"
    return 1
  fi

  echo ""
  # 7. Show AFTER state
  rich --print "[bold magenta]--- After ExifTool Modifications ---[/bold magenta]"
  exiftool -time:all -G1 -a "$target_path"
  echo ""
}
