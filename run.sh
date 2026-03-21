#!/usr/bin/env bash
# run.sh — Quick start menu for the Polymarket oracle lag bot
set -e

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; NC='\033[0m'

banner() {
  echo -e "${CYAN}"
  echo "  ╔══════════════════════════════════════════╗"
  echo "  ║   Polymarket BTC Oracle Lag Bot          ║"
  echo "  ╚══════════════════════════════════════════╝"
  echo -e "${NC}"
}

check_env() {
  if [ ! -f .env ]; then
    echo -e "${YELLOW}No .env file found. Running credential setup...${NC}"
    python setup_creds.py
  fi
  if [ -z "$VIRTUAL_ENV" ] && [ -d "venv" ]; then
    echo -e "${YELLOW}Activating venv...${NC}"
    source venv/bin/activate
  fi
}

install_deps() {
  echo -e "${BLUE}Installing dependencies...${NC}"
  pip install -r requirements.txt -q
  echo -e "${GREEN}Done.${NC}"
}

banner

echo "Choose an action:"
echo "  1) Install dependencies"
echo "  2) Setup credentials"
echo "  3) Watch oracle lag signals (live, no money)"
echo "  4) Backtest (48h, ultra mode)"
echo "  5) Paper trade (dry run)"
echo "  6) Launch dashboard (dry run)"
echo "  7) LIVE trade — oracle lag only"
echo "  8) LIVE trade — ultra mode (all signals)"
echo ""
read -p "Enter choice [1-8]: " choice

case $choice in
  1)
    install_deps
    ;;
  2)
    python setup_creds.py
    ;;
  3)
    check_env
    echo -e "${CYAN}Watching for oracle lag signals... (Ctrl+C to stop)${NC}"
    python oracle_lag.py
    ;;
  4)
    check_env
    echo -e "${CYAN}Running backtest...${NC}"
    python backtest.py --hours 48 --mode ultra
    ;;
  5)
    check_env
    echo -e "${CYAN}Paper trading — no real money${NC}"
    python bot.py --dry-run --mode ultra --strategy oracle_lag --verbose
    ;;
  6)
    check_env
    echo -e "${CYAN}Dashboard at http://localhost:5000${NC}"
    python server.py --dry-run --mode ultra
    ;;
  7)
    check_env
    echo -e "${RED}WARNING: This will trade with REAL money.${NC}"
    read -p "Type 'yes' to confirm: " confirm
    if [ "$confirm" = "yes" ]; then
      python bot.py --mode ultra --strategy oracle_lag
    else
      echo "Cancelled."
    fi
    ;;
  8)
    check_env
    echo -e "${RED}WARNING: This will trade with REAL money (ultra mode).${NC}"
    read -p "Type 'yes' to confirm: " confirm
    if [ "$confirm" = "yes" ]; then
      python bot.py --mode ultra
    else
      echo "Cancelled."
    fi
    ;;
  *)
    echo "Invalid choice."
    ;;
esac
