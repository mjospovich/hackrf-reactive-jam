#!/usr/bin/env python3
"""
Quick test script to verify dual HackRF setup.
Run this before using the reactive jammer to ensure both devices are detected.
"""

import sys
import subprocess

def check_hackrf_devices():
    """Check if HackRF devices are properly detected"""
    print("="*60)
    print("HackRF Device Detection Test")
    print("="*60)
    
    try:
        result = subprocess.run(['hackrf_info'], capture_output=True, text=True)
        output = result.stdout + result.stderr
        
        print(output)
        
        # Count devices
        device_count = output.count('Serial number:')
        
        print("="*60)
        print(f"Detected {device_count} HackRF device(s)")
        
        if device_count < 2:
            print("\n[WARNING] Need 2 HackRF devices for reactive jamming!")
            print("          Make sure both are connected and recognized.")
            return False
        else:
            print("\n[OK] Both HackRF devices detected!")
            return True
            
    except FileNotFoundError:
        print("[ERROR] hackrf_info command not found!")
        print("        Install HackRF tools: sudo apt install hackrf")
        return False
    except Exception as e:
        print(f"[ERROR] {e}")
        return False


def test_gnuradio():
    """Test if GNU Radio is available"""
    print("\n" + "="*60)
    print("GNU Radio Import Test")
    print("="*60)
    
    try:
        from gnuradio import gr
        print(f"[OK] GNU Radio version: {gr.version()}")
        return True
    except ImportError as e:
        print(f"[ERROR] Cannot import GNU Radio: {e}")
        return False


def test_osmosdr():
    """Test if OsmoSDR is available"""
    print("\n" + "="*60)
    print("OsmoSDR Import Test")
    print("="*60)
    
    try:
        import osmosdr
        print("[OK] OsmoSDR imported successfully")
        return True
    except ImportError as e:
        print(f"[ERROR] Cannot import osmosdr: {e}")
        print("        Install: sudo apt install gr-osmosdr")
        return False


def main():
    print("""
    ╔══════════════════════════════════════════════════════════════╗
    ║              HackRF Dual Device Setup Test                  ║
    ╚══════════════════════════════════════════════════════════════╝
    """)
    
    results = []
    
    results.append(("HackRF Devices", check_hackrf_devices()))
    results.append(("GNU Radio", test_gnuradio()))
    results.append(("OsmoSDR", test_osmosdr()))
    
    print("\n" + "="*60)
    print("SUMMARY")
    print("="*60)
    
    all_ok = True
    for name, status in results:
        status_str = "[OK]" if status else "[FAIL]"
        print(f"  {status_str} {name}")
        all_ok = all_ok and status
    
    print("="*60)
    
    if all_ok:
        print("\n[SUCCESS] All tests passed! Ready for reactive jamming.")
        print("\nRecommended command:")
        print("  python reactive_jammer.py --wideband")
        print("\nOr for maximum reliability:")
        print("  python reactive_jammer.py --continuous")
    else:
        print("\n[FAILED] Some tests failed. Fix issues before proceeding.")
        sys.exit(1)


if __name__ == "__main__":
    main()
