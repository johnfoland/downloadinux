# downloadinux functions
# Ported verbatim from ~/.oh-my-zsh/custom/functions.zsh

# -------------------------------------------------------------------------------------------------
# Core Batch Downloader Helper
# Usage: _batch_downloader_core <input_file> <log_prefix> <job_title> <color> <command_array...>
# -------------------------------------------------------------------------------------------------

_batch_downloader_core() {
  local input_file="$1"; shift
  local log_prefix="$1"; shift
  local job_title="$1"; shift
  local color="$1"; shift
  local cmd=("$@")

  # 2. SILENT EXIT: Check if file is completely empty, or only contains whitespace/comments
  if ! grep -q -v '^[[:space:]]*\(#\|$\)' "$input_file"; then
    return 0
  fi

  # 3. Setup Log Files (Written to the log_prefix directory)
  local timestamp=$(date +%Y%m%d_%H%M%S)
  local log_dir="$PWD/${log_prefix}"

  # Ensure the target directory exists
  mkdir -p "$log_dir"

  local fail_log="${log_dir}/${log_prefix}_failed_${timestamp}.txt"
  local success_log="${log_dir}/${log_prefix}_successful_${timestamp}.txt"

  local failed_items=()
  local success_items=()

  echo ""
  rich --print "[bold ${color}]Starting ${job_title}[/bold ${color}]
Reading from: [b]${input_file}[/b]" --panel rounded --style "${color}"

  # 4. Read file line by line
  while IFS= read -r line || [[ -n "$line" ]]; do
    # Skip empty lines and comments
    [[ -z "$line" ]] || [[ "$line" =~ ^#.*$ ]] && continue

    echo ""
    rich --print "[bold yellow]Processing:[/bold yellow] $line"

    # Execute the passed tool and arguments against the URL
    if ! "${cmd[@]}" "$line"; then
      failed_items+=("$line")
      echo "$line" >> "$fail_log"
      rich --print "[bold red]✗ Failed to download.[/bold red] Logged to failed file."
    else
      success_items+=("$line")
      echo "$line" >> "$success_log"

      # Safely remove the successful URL from the input file
      # Use `|| true` because grep exits with 1 if the output file is completely empty
      grep -v -x -F "$line" "$input_file" > "${input_file}.tmp" || true
      mv "${input_file}.tmp" "$input_file"

      rich --print "[bold green]✓ Successfully downloaded.[/bold green] Removed from input file."
    fi

    # 5. Inter-URL Throttling (Triggered regardless of success/fail)
    # Skip the sleep if the queue is now empty
    if [[ -s "$input_file" ]]; then
      # Sleep for a random interval between 120 and 289 seconds (two minutes to four minutes & 49 seconds)
      local random_sleep=$(( (RANDOM % 170) + 120 ))
      rich --print "[dim]Throttling for ${random_sleep} seconds to mimic human behavior...[/dim]"
      sleep "$random_sleep"
    fi

  done < "$input_file"

  # 6. Final Report
  echo ""
  if (( ${#failed_items[@]} > 0 )); then
    rich --print "[bold red]${job_title} Complete with Errors[/bold red]
[b]${#failed_items[@]}[/b] failed | [b]${#success_items[@]}[/b] succeeded.
See [dim]${fail_log}[/dim] for failures." --panel double --style red
  else
    rich --print "[bold green]${job_title} Complete![/bold green]
All [b]${#success_items[@]}[/b] items downloaded successfully.
Logs saved to [dim]${success_log}[/dim]" --panel double --style green
  fi
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

  _batch_downloader_core "$1" "dlmp4" "Batch Download" "blue" yt-dlp --config-location ~/.config/yt-dlp/dlmp4.conf
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

  _batch_downloader_core "$1" "dlgal" "Gallery Batch Download" "magenta" gallery-dl -q
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
