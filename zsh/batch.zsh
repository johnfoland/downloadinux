# downloadinux batch loop
# `dlx batch` runs this file alone under `zsh -f` in a tmux session; the
# interactive shell sources it with the other zsh/ files (docs/target-design.md,
# Container contract, "Pinned for P0001-C04"). Ported verbatim from
# ~/.oh-my-zsh/custom/functions.zsh.

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
