#!/bin/bash
# =============================================================================
# HackRF Reactive Jammer - Setup & Launch Script
# =============================================================================
# T.A.R.S. Setup Assistant
# FOR EDUCATIONAL/RESEARCH USE IN CONTROLLED LAB ENVIRONMENT ONLY
# =============================================================================

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"

# =============================================================================
# Helper Functions
# =============================================================================

print_header() {
    echo -e "${CYAN}"
    echo "╔══════════════════════════════════════════════════════════════╗"
    echo "║           HackRF Reactive Jammer - T.A.R.S. Control          ║"
    echo "║                                                              ║"
    echo "║   FOR EDUCATIONAL/RESEARCH USE IN CONTROLLED LAB ONLY        ║"
    echo "╚══════════════════════════════════════════════════════════════╝"
    echo -e "${NC}"
}

print_section() {
    echo -e "\n${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${BOLD}$1${NC}"
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}\n"
}

success() { echo -e "${GREEN}[✓]${NC} $1"; }
warn()    { echo -e "${YELLOW}[!]${NC} $1"; }
error()   { echo -e "${RED}[✗]${NC} $1"; }
info()    { echo -e "${CYAN}[i]${NC} $1"; }

# =============================================================================
# System Dependency Checks
# =============================================================================

check_system_deps() {
    print_section "Checking System Dependencies"
    
    local all_ok=true
    
    if command -v python3 &> /dev/null; then
        success "Python 3 found: $(python3 --version 2>&1)"
    else
        error "Python 3 not found!"
        all_ok=false
    fi
    
    if command -v hackrf_info &> /dev/null; then
        success "HackRF tools installed"
    else
        error "HackRF tools not found!"
        warn "Install: brew install hackrf (macOS) or sudo apt install hackrf (Linux)"
        all_ok=false
    fi
    
    if python3 -c "from gnuradio import gr" &> /dev/null; then
        local gr_version=$(python3 -c "from gnuradio import gr; print(gr.version())" 2>/dev/null || echo "unknown")
        success "GNU Radio found: $gr_version"
    else
        error "GNU Radio not found!"
        warn "Install: brew install gnuradio (macOS) or sudo apt install gnuradio (Linux)"
        all_ok=false
    fi
    
    if python3 -c "import osmosdr" &> /dev/null; then
        success "gr-osmosdr found"
    else
        error "gr-osmosdr not found!"
        warn "Install: brew install gr-osmosdr (macOS) or sudo apt install gr-osmosdr (Linux)"
        all_ok=false
    fi
    
    echo ""
    if [ "$all_ok" = true ]; then
        success "All system dependencies satisfied"
        return 0
    else
        error "Some system dependencies are missing"
        return 1
    fi
}

# =============================================================================
# Virtual Environment Setup
# =============================================================================

setup_venv() {
    print_section "Setting Up Python Virtual Environment"
    
    if [ -d "$VENV_DIR" ]; then
        info "Virtual environment already exists at $VENV_DIR"
        read -p "Recreate it? [y/N]: " recreate
        if [[ "$recreate" =~ ^[Yy]$ ]]; then
            rm -rf "$VENV_DIR"
        else
            success "Using existing virtual environment"
            return 0
        fi
    fi
    
    info "Creating virtual environment..."
    python3 -m venv "$VENV_DIR"
    success "Virtual environment created"
    
    source "$VENV_DIR/bin/activate"
    
    info "Upgrading pip..."
    pip install --upgrade pip -q
    
    info "Installing Python dependencies..."
    pip install -r "$SCRIPT_DIR/requirements.txt" -q
    success "Dependencies installed"
    
    # Link GNU Radio system packages into venv
    local site_packages="$VENV_DIR/lib/python$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')/site-packages"
    local sys_site=$(python3 -c "import site; print(site.getsitepackages()[0])" 2>/dev/null || echo "")
    
    if [ -n "$sys_site" ] && [ -d "$sys_site" ]; then
        echo "$sys_site" > "$site_packages/system_packages.pth"
        info "Added system site-packages path for GNU Radio access"
    fi
    
    deactivate
    success "Virtual environment setup complete"
}

activate_venv() {
    if [ -d "$VENV_DIR" ]; then
        source "$VENV_DIR/bin/activate"
        return 0
    else
        error "Virtual environment not found. Run setup first."
        return 1
    fi
}

# =============================================================================
# Run Options
# =============================================================================

run_device_test() {
    print_section "Running HackRF Device Test"
    activate_venv || return 1
    cd "$SCRIPT_DIR"
    python3 test_hackrf_devices.py
    deactivate
}

run_jammer() {
    print_section "Fast Reactive Jammer"
    activate_venv || return 1
    
    echo -e "${YELLOW}Options:${NC}"
    echo "  1) Normal start (with calibration)"
    echo "  2) Skip calibration (use defaults)"
    echo "  3) Back to menu"
    echo ""
    read -p "Select [1-3]: " opt
    
    cd "$SCRIPT_DIR"
    
    case $opt in
        1)
            info "Starting jammer with calibration..."
            python3 fast_reactive_jammer.py
            ;;
        2)
            info "Starting jammer (skipping calibration)..."
            python3 fast_reactive_jammer.py --skip-cal
            ;;
        3)
            deactivate
            return 0
            ;;
        *)
            warn "Invalid option"
            ;;
    esac
    
    deactivate
}

# =============================================================================
# Main Menu
# =============================================================================

show_menu() {
    echo ""
    echo -e "${BOLD}Main Menu${NC}"
    echo -e "${BLUE}─────────────────────────────────────────${NC}"
    echo "  1) Setup environment"
    echo "  2) Check system dependencies"
    echo -e "${BLUE}─────────────────────────────────────────${NC}"
    echo "  3) Test HackRF devices"
    echo "  4) Run Reactive Jammer"
    echo -e "${BLUE}─────────────────────────────────────────${NC}"
    echo "  0) Exit"
    echo ""
}

main_menu() {
    while true; do
        show_menu
        read -p "Commander, your orders? [0-4]: " choice
        
        case $choice in
            1) setup_venv ;;
            2) check_system_deps ;;
            3) run_device_test ;;
            4) run_jammer ;;
            0)
                echo ""
                info "Acknowledged, Commander. Systems shutting down."
                echo ""
                exit 0
                ;;
            *)
                warn "Invalid option."
                ;;
        esac
        
        echo ""
        read -p "Press Enter to continue..."
    done
}

# =============================================================================
# CLI Arguments
# =============================================================================

show_help() {
    echo "Usage: $0 [COMMAND]"
    echo ""
    echo "Commands:"
    echo "  setup    Setup virtual environment and install dependencies"
    echo "  check    Check system dependencies"
    echo "  test     Run HackRF device test"
    echo "  jammer   Run reactive jammer"
    echo "  menu     Interactive menu (default)"
    echo "  help     Show this help"
}

# =============================================================================
# Entry Point
# =============================================================================

print_header

case "${1:-menu}" in
    setup)        check_system_deps; setup_venv ;;
    check)        check_system_deps ;;
    test)         run_device_test ;;
    jammer|jam)   run_jammer ;;
    menu|"")      main_menu ;;
    help|--help|-h) show_help ;;
    *)
        error "Unknown command: $1"
        show_help
        exit 1
        ;;
esac
