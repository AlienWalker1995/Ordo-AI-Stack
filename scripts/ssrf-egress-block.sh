#!/usr/bin/env bash
# Block MCP gateway and OpenClaw browser-tier containers from reaching private ranges
# and cloud metadata. Reduces SSRF risk. See docs/runbooks/SECURITY_HARDENING.md.
#
# Usage:
#   ./scripts/ssrf-egress-block.sh                        # block MCP subnet (default)
#   ./scripts/ssrf-egress-block.sh --target openclaw      # block openclaw subnet
#   ./scripts/ssrf-egress-block.sh --target all           # block both MCP + openclaw
#   ./scripts/ssrf-egress-block.sh --dry-run              # print commands only
#   ./scripts/ssrf-egress-block.sh --remove               # remove rules for targeted subnet(s)
#   ./scripts/ssrf-egress-block.sh 172.18.0.0/16          # explicit subnet override
#
# Persistence: apt install iptables-persistent && sudo netfilter-persistent save

set -e

DRY_RUN=false
REMOVE=false
TARGET="mcp"   # default: mcp | openclaw | all
SUBNET_OVERRIDE=""

for arg in "$@"; do
  case "$arg" in
    --dry-run)  DRY_RUN=true ;;
    --remove)   REMOVE=true ;;
    --target)   ;;  # consumed by next iteration via shift pattern below
    -h|--help)
      echo "Usage: $0 [--dry-run] [--remove] [--target mcp|openclaw|all] [SUBNET]"
      echo "  --target mcp        Block MCP gateway subnet (default)"
      echo "  --target openclaw   Block OpenClaw browser-tier subnet"
      echo "  --target all        Block both subnets"
      echo "  SUBNET              Explicit subnet override (e.g. 172.18.0.0/16)"
      exit 0
      ;;
    --target=*) TARGET="${arg#--target=}" ;;
    *) [ -z "$SUBNET_OVERRIDE" ] && SUBNET_OVERRIDE="$arg" ;;
  esac
done

# Handle --target as a separate positional argument (--target foo)
# Re-parse to handle space-separated --target value
ARGS=("$@")
for i in "${!ARGS[@]}"; do
  if [ "${ARGS[$i]}" = "--target" ] && [ -n "${ARGS[$((i+1))+_}" ]; then
    TARGET="${ARGS[$((i+1))]}"
  fi
done

detect_subnet() {
  local network_name="$1"
  local subnet=""
  if command -v docker >/dev/null 2>&1; then
    subnet=$(docker network inspect "$network_name" 2>/dev/null \
      | jq -r '.[0].IPAM.Config[0].Subnet // empty' 2>/dev/null || true)
  fi
  echo "$subnet"
}

get_subnet_for_target() {
  local target="$1"
  local subnet=""

  if [ -n "$SUBNET_OVERRIDE" ]; then
    echo "$SUBNET_OVERRIDE"
    return
  fi

  case "$target" in
    mcp)
      subnet=$(detect_subnet "ordo-ai-stack-frontend")
      [ -z "$subnet" ] && subnet=$(detect_subnet "ordo-ai-stack_default")
      ;;
    openclaw)
      subnet=$(detect_subnet "ordo-ai-stack-openclaw")
      # Fall back to frontend if no dedicated openclaw network exists yet
      [ -z "$subnet" ] && subnet=$(detect_subnet "ordo-ai-stack-frontend")
      [ -z "$subnet" ] && subnet=$(detect_subnet "ordo-ai-stack_default")
      ;;
  esac

  echo "$subnet"
}

RUN() {
  if [ "$DRY_RUN" = true ]; then
    echo "Would run: $*"
  else
    "$@"
  fi
}

apply_rules() {
  local subnet="$1"
  local label="$2"
  echo "Adding egress blocks for $label (subnet $subnet): RFC1918, Tailscale, metadata..."
  RUN sudo iptables -I DOCKER-USER -s "$subnet" -d 10.0.0.0/8       -j DROP
  RUN sudo iptables -I DOCKER-USER -s "$subnet" -d 172.16.0.0/12    -j DROP
  RUN sudo iptables -I DOCKER-USER -s "$subnet" -d 192.168.0.0/16   -j DROP
  RUN sudo iptables -I DOCKER-USER -s "$subnet" -d 100.64.0.0/10    -j DROP
  RUN sudo iptables -I DOCKER-USER -s "$subnet" -d 169.254.169.254/32 -j DROP
  RUN sudo iptables -I DOCKER-USER -s "$subnet" -d 169.254.170.2/32   -j DROP
  # Allow DNS so tool containers can resolve external hostnames
  RUN sudo iptables -I DOCKER-USER -s "$subnet" -p udp --dport 53   -j ACCEPT
  RUN sudo iptables -I DOCKER-USER -s "$subnet" -p tcp --dport 53   -j ACCEPT
}

remove_rules() {
  local subnet="$1"
  local label="$2"
  echo "Removing DOCKER-USER rules for $label (source $subnet)..."
  for _ in 1 2 3 4 5 6 7 8; do
    sudo iptables -D DOCKER-USER -s "$subnet" -d 10.0.0.0/8       -j DROP 2>/dev/null || true
    sudo iptables -D DOCKER-USER -s "$subnet" -d 172.16.0.0/12    -j DROP 2>/dev/null || true
    sudo iptables -D DOCKER-USER -s "$subnet" -d 192.168.0.0/16   -j DROP 2>/dev/null || true
    sudo iptables -D DOCKER-USER -s "$subnet" -d 100.64.0.0/10    -j DROP 2>/dev/null || true
    sudo iptables -D DOCKER-USER -s "$subnet" -d 169.254.169.254/32 -j DROP 2>/dev/null || true
    sudo iptables -D DOCKER-USER -s "$subnet" -d 169.254.170.2/32   -j DROP 2>/dev/null || true
    sudo iptables -D DOCKER-USER -s "$subnet" -p udp --dport 53   -j ACCEPT 2>/dev/null || true
    sudo iptables -D DOCKER-USER -s "$subnet" -p tcp --dport 53   -j ACCEPT 2>/dev/null || true
  done
}

process_target() {
  local target="$1"
  local subnet
  subnet=$(get_subnet_for_target "$target")

  if [ -z "$subnet" ]; then
    echo "Could not detect subnet for target '$target'. Start the stack once (docker compose up -d), or pass an explicit SUBNET." >&2
    exit 1
  fi

  if [ "$REMOVE" = true ]; then
    remove_rules "$subnet" "$target"
  else
    apply_rules "$subnet" "$target"
  fi
}

case "$TARGET" in
  mcp)      process_target "mcp" ;;
  openclaw) process_target "openclaw" ;;
  all)
    process_target "mcp"
    process_target "openclaw"
    ;;
  *)
    echo "Unknown --target '$TARGET'. Use: mcp | openclaw | all" >&2
    exit 1
    ;;
esac

echo "Done. Verify: sudo iptables -L DOCKER-USER -n -v"
if [ "$REMOVE" = false ]; then
  echo "To persist (Debian/Ubuntu): sudo apt install iptables-persistent && sudo netfilter-persistent save"
fi
