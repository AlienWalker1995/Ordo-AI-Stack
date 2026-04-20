#!/usr/bin/env bash
# Block MCP gateway containers from reaching private ranges
# and cloud metadata. Reduces SSRF risk. See docs/runbooks/SECURITY_HARDENING.md.
#
# Usage:
#   ./scripts/ssrf-egress-block.sh                        # block MCP subnet
#   ./scripts/ssrf-egress-block.sh --dry-run              # print commands only
#   ./scripts/ssrf-egress-block.sh --remove               # remove rules
#   ./scripts/ssrf-egress-block.sh 172.18.0.0/16          # explicit subnet override
#
# Persistence: apt install iptables-persistent && sudo netfilter-persistent save

set -e

DRY_RUN=false
REMOVE=false
SUBNET_OVERRIDE=""

for arg in "$@"; do
  case "$arg" in
    --dry-run)  DRY_RUN=true ;;
    --remove)   REMOVE=true ;;
    -h|--help)
      echo "Usage: $0 [--dry-run] [--remove] [SUBNET]"
      echo "  SUBNET              Explicit subnet override (e.g. 172.18.0.0/16)"
      exit 0
      ;;
    *) [ -z "$SUBNET_OVERRIDE" ] && SUBNET_OVERRIDE="$arg" ;;
  esac
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

get_subnet() {
  local subnet=""

  if [ -n "$SUBNET_OVERRIDE" ]; then
    echo "$SUBNET_OVERRIDE"
    return
  fi

  subnet=$(detect_subnet "ordo-ai-stack-frontend")
  [ -z "$subnet" ] && subnet=$(detect_subnet "ordo-ai-stack_default")

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

subnet=$(get_subnet)

if [ -z "$subnet" ]; then
  echo "Could not detect subnet for MCP gateway. Start the stack once (docker compose up -d), or pass an explicit SUBNET." >&2
  exit 1
fi

if [ "$REMOVE" = true ]; then
  remove_rules "$subnet" "mcp"
else
  apply_rules "$subnet" "mcp"
fi

echo "Done. Verify: sudo iptables -L DOCKER-USER -n -v"
if [ "$REMOVE" = false ]; then
  echo "To persist (Debian/Ubuntu): sudo apt install iptables-persistent && sudo netfilter-persistent save"
fi
