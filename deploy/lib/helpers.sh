#!/usr/bin/env bash
# Shared helpers for Aurora deployment scripts.
# Source this file: source "$(dirname "$0")/lib/helpers.sh"

info() { echo -e "\033[1;34m→\033[0m $1"; }
ok()   { echo -e "\033[1;32m✓\033[0m $1"; }
warn() { echo -e "\033[1;33m!\033[0m $1"; }

check_tool() {
  command -v "$1" &>/dev/null
}

# Arrow-key navigable menu. Sets MENU_RESULT to the 0-based index chosen.
# Usage: select_menu "Prompt" "Option A" "Option B" "Option C"
select_menu() {
  local prompt="$1"; shift
  local options=("$@")
  local count=${#options[@]}
  local selected=0

  printf "\033[?25l"
  trap 'printf "\033[?25h"' EXIT INT TERM

  local _menu_drawn=0
  local draw_menu
  draw_menu() {
    if [ "$_menu_drawn" -eq 1 ]; then
      printf "\033[%dA" "$count"
    fi
    for i in "${!options[@]}"; do
      if [ "$i" -eq "$selected" ]; then
        printf "\033[2K  \033[1;36m❯ %s\033[0m\n" "${options[$i]}"
      else
        printf "\033[2K    %s\n" "${options[$i]}"
      fi
    done
    _menu_drawn=1
  }

  echo "$prompt"
  echo ""
  draw_menu

  while true; do
    IFS= read -rsn1 key
    case "$key" in
      $'\x1b')
        read -rsn2 seq
        case "$seq" in
          '[A') (( selected > 0 )) && (( selected-- )); draw_menu ;;
          '[B') (( selected < count - 1 )) && (( selected++ )); draw_menu ;;
        esac
        ;;
      '') break ;;
    esac
  done

  printf "\033[?25h"
  echo ""
  MENU_RESULT=$selected
}

# Prompt with a default value. Sets PROMPT_RESULT.
# Usage: prompt_default "Question" "default_value"
prompt_default() {
  local question="$1"
  local default="$2"
  if [ -t 0 ]; then
    printf "%s [%s]: " "$question" "$default"
    read -r PROMPT_RESULT
    PROMPT_RESULT="${PROMPT_RESULT:-$default}"
  else
    PROMPT_RESULT="$default"
  fi
}

# Confirm yes/no. Returns 0 for yes, 1 for no.
# Usage: confirm "Question" && do_thing
confirm() {
  local question="$1"
  if [ -t 0 ]; then
    printf "%s [Y/n]: " "$question"
    read -r answer
    answer="${answer:-Y}"
    [[ "$answer" =~ ^[Yy] ]]
  else
    return 0
  fi
}
