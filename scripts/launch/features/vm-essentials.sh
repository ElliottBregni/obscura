# DESC: Install commonly-useful CLI tools in the VM (jq, ripgrep, fd, tmux, ...)
# Sourced by colima-feature.sh.
#
# Idempotent: safe to enable repeatedly. Listed packages are chosen for what
# obscura agents and you tend to reach for inside the VM:
#   build basics  : build-essential, ca-certificates, curl, git, pkg-config
#   shell QoL     : jq, ripgrep, fd-find, fzf, tmux, htop, less, unzip
#   network probe : socat, netcat-openbsd, dnsutils, iproute2
#   python build  : python3-dev (for any C-extension wheels uv may compile)
#
# Add OBSCURA_VM_EXTRA_APT="pkg1 pkg2" to install more.

VM_BASE_PKGS=(
  build-essential ca-certificates curl git pkg-config
  jq ripgrep fd-find fzf tmux htop less unzip
  socat netcat-openbsd dnsutils iproute2
  python3-dev
)

feature_enable() {
  echo "⟳ Installing VM essentials..."
  local extras="${OBSCURA_VM_EXTRA_APT:-}"
  ssh -F "$SSH_CONFIG" "$SSH_HOST" bash <<EOF
    set -e
    sudo apt-get update -qq
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \\
      ${VM_BASE_PKGS[*]} $extras
    # Ubuntu ships fd as fdfind; symlink to fd if not already there.
    if command -v fdfind &>/dev/null && ! command -v fd &>/dev/null; then
      sudo ln -sf "\$(command -v fdfind)" /usr/local/bin/fd
    fi
    echo "✓ VM essentials installed"
EOF
}

feature_disable() {
  echo "⚠️  vm-essentials installs packages; refusing to auto-uninstall."
  echo "   If you really want to remove them, ssh in and run apt remove yourself."
  return 0
}

feature_status() {
  ssh -F "$SSH_CONFIG" "$SSH_HOST" bash <<'EOF' 2>/dev/null || echo "  (VM unreachable)"
    missing=()
    for pkg in jq ripgrep fdfind fzf tmux htop socat nc dig; do
      command -v "$pkg" &>/dev/null || missing+=("$pkg")
    done
    if [[ ${#missing[@]} -eq 0 ]]; then
      echo "  all essentials present"
    else
      echo "  missing: ${missing[*]}"
    fi
EOF
}
