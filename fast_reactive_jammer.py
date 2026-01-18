#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FAST REACTIVE JAMMER - Optimized for DJI 2.4GHz FHSS
====================================================

Architecture:
- RX Thread: Continuous rapid sweep across 100MHz band (~50ms full sweep)
- TX Thread: Instant response jamming at detected frequency
- Communication: Lock-free queue for minimum latency

Target: DJI Mini 2 SE (OcuSync 2.0, 2.4GHz band, ~100MHz, 15-20MHz channels)
Hardware: 2x HackRF One

For educational/research use in controlled lab environment only.
"""

import os
import sys
import time
import yaml
import signal
import osmosdr
import threading
import numpy as np
from collections import deque
from gnuradio.fft import window
from gnuradio import gr, blocks, analog, fft

# =============================================================================
# CONFIGURATION - Defaults (can be overridden by config.yaml)
# =============================================================================

class Config:
    """
    Configuration with hardcoded defaults.
    Values can be overridden by config.yaml if present.
    """
    # Device assignment
    RX_DEVICE = "hackrf=0"
    TX_DEVICE = "hackrf=1"
    
    # Frequency band (DJI 2.4GHz)
    FREQ_MIN = 2.400e9
    FREQ_MAX = 2.4835e9
    BAND_WIDTH = FREQ_MAX - FREQ_MIN  # ~83.5 MHz
    
    # HackRF settings
    SAMPLE_RATE = 20e6
    BANDWIDTH = 20e6
    FFT_SIZE = 512  # Smaller = faster, less resolution (tradeoff)
    
    # Sweep configuration
    # 5 chunks to cover ~100MHz with 20MHz each (some overlap)
    SWEEP_FREQS = [
        2.410e9,  # Covers 2400-2420
        2.430e9,  # Covers 2420-2440  
        2.450e9,  # Covers 2440-2460
        2.470e9,  # Covers 2460-2480
        2.490e9,  # Covers 2480-2500 (partial)
    ]
    
    # Timing (CRITICAL for speed)
    RX_DWELL_TIME = 0.008      # 8ms per frequency chunk (40ms full sweep)
    TX_JAM_DURATION = 0.015    # 15ms minimum jam burst
    TX_HOLDOFF = 0.002         # 2ms after jam before allowing re-detection
    
    # Detection
    THRESHOLD_MARGIN_DB = 8    # dB above noise floor
    CALIBRATION_SAMPLES = 50   # Number of samples for noise floor
    
    # TX Power
    TX_POWER_DBM = 10
    
    # Runtime
    TOTAL_DURATION = 300


def load_config(config_file="config.yaml"):
    """
    Load configuration from YAML file.
    If file doesn't exist or a value is missing, defaults are used.
    Returns a Config object with loaded/default values.
    """
    config = Config()
    
    # Find config file relative to script location
    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(script_dir, config_file)
    
    if not os.path.exists(config_path):
        print(f"[CONFIG] {config_file} not found, using defaults")
        return config
    
    try:
        with open(config_path, 'r') as f:
            yaml_config = yaml.safe_load(f)
        
        if yaml_config is None:
            print(f"[CONFIG] {config_file} is empty, using defaults")
            return config
        
        # Device assignment
        if 'rx_device' in yaml_config:
            config.RX_DEVICE = yaml_config['rx_device']
        if 'tx_device' in yaml_config:
            config.TX_DEVICE = yaml_config['tx_device']
        
        # Frequency band (convert MHz to Hz)
        if 'freq_min' in yaml_config:
            config.FREQ_MIN = yaml_config['freq_min'] * 1e6
        if 'freq_max' in yaml_config:
            config.FREQ_MAX = yaml_config['freq_max'] * 1e6
        config.BAND_WIDTH = config.FREQ_MAX - config.FREQ_MIN
        
        # HackRF settings (convert MHz to Hz where needed)
        if 'sample_rate' in yaml_config:
            config.SAMPLE_RATE = yaml_config['sample_rate'] * 1e6
        if 'bandwidth' in yaml_config:
            config.BANDWIDTH = yaml_config['bandwidth'] * 1e6
        if 'fft_size' in yaml_config:
            config.FFT_SIZE = yaml_config['fft_size']
        
        # Sweep frequencies (convert MHz to Hz)
        if 'sweep_freqs' in yaml_config:
            config.SWEEP_FREQS = [f * 1e6 for f in yaml_config['sweep_freqs']]
        
        # Timing
        if 'rx_dwell_time' in yaml_config:
            config.RX_DWELL_TIME = yaml_config['rx_dwell_time']
        if 'tx_jam_duration' in yaml_config:
            config.TX_JAM_DURATION = yaml_config['tx_jam_duration']
        if 'tx_holdoff' in yaml_config:
            config.TX_HOLDOFF = yaml_config['tx_holdoff']
        
        # Detection
        if 'threshold_margin_db' in yaml_config:
            config.THRESHOLD_MARGIN_DB = yaml_config['threshold_margin_db']
        if 'calibration_samples' in yaml_config:
            config.CALIBRATION_SAMPLES = yaml_config['calibration_samples']
        
        # TX Power
        if 'tx_power_dbm' in yaml_config:
            config.TX_POWER_DBM = yaml_config['tx_power_dbm']
        
        # Runtime
        if 'duration' in yaml_config:
            config.TOTAL_DURATION = yaml_config['duration']
        
        print(f"[CONFIG] Loaded from {config_file}")
        
    except yaml.YAMLError as e:
        print(f"[CONFIG] Error parsing {config_file}: {e}")
        print("[CONFIG] Using defaults")
    except Exception as e:
        print(f"[CONFIG] Error loading {config_file}: {e}")
        print("[CONFIG] Using defaults")
    
    return config


# =============================================================================
# FAST SPECTRUM MONITOR (RX)
# =============================================================================

class FastSpectrumMonitor(gr.top_block):
    """
    Optimized spectrum monitor that stays running.
    Uses probe for non-blocking spectrum retrieval.
    Retuning via set_center_freq() is much faster than restart.
    """
    
    def __init__(self, device, sample_rate, bandwidth, fft_size):
        gr.top_block.__init__(self, "FastRX")
        
        self.sample_rate = sample_rate
        self.fft_size = fft_size
        
        # HackRF Source
        self.source = osmosdr.source(args=device)
        self.source.set_sample_rate(sample_rate)
        self.source.set_center_freq(Config.SWEEP_FREQS[0], 0)
        self.source.set_freq_corr(0, 0)
        self.source.set_gain(0, 0)
        self.source.set_if_gain(40, 0)  # High gain for sensitivity
        self.source.set_bb_gain(32, 0)
        self.source.set_bandwidth(bandwidth, 0)
        
        # Stream to vector
        self.s2v = blocks.stream_to_vector(gr.sizeof_gr_complex, fft_size)
        
        # FFT with Blackman-Harris window (good sidelobe suppression)
        self.fft_block = fft.fft_vcc(
            fft_size, 
            True,  # Forward FFT
            window.blackmanharris(fft_size),
            True   # Shift
        )
        
        # Magnitude squared
        self.mag2 = blocks.complex_to_mag_squared(fft_size)
        
        # Probe for non-blocking access
        self.probe = blocks.probe_signal_vf(fft_size)
        
        # Connect
        self.connect(self.source, self.s2v, self.fft_block, self.mag2, self.probe)
    
    def retune(self, freq):
        """Fast retune without stopping flowgraph"""
        self.source.set_center_freq(freq, 0)
    
    def get_power(self):
        """Get current spectrum power (fast)"""
        try:
            spectrum = self.probe.level()
            if len(spectrum) > 0:
                return np.mean(spectrum)
            return 0.0
        except:
            return 0.0
    
    def get_spectrum(self):
        """Get full spectrum array"""
        try:
            return np.array(self.probe.level())
        except:
            return np.array([])


# =============================================================================
# FAST JAMMER (TX)  
# =============================================================================

class FastJammer(gr.top_block):
    """
    Pre-initialized jammer that stays running.
    Uses valve to control TX without flowgraph restart.
    Retuning is instant via set_center_freq().
    """
    
    def __init__(self, device, sample_rate, bandwidth, power_dbm):
        gr.top_block.__init__(self, "FastTX")
        
        self.sample_rate = sample_rate
        self.is_transmitting = False
        
        # Calculate gains
        rf_gain, if_gain = self._calc_gains(power_dbm)
        
        # Wideband noise source (most effective for FHSS)
        self.source = analog.noise_source_c(analog.GR_GAUSSIAN, 1.0, 0)
        
        # Valve to control transmission (0 = blocked, >0 = passing)
        # Using multiply by constant as simple gate
        self.gate = blocks.multiply_const_cc(0.0)  # Start with TX off
        
        # HackRF Sink
        self.sink = osmosdr.sink(args=device)
        self.sink.set_sample_rate(sample_rate)
        self.sink.set_center_freq(Config.SWEEP_FREQS[0], 0)
        self.sink.set_freq_corr(0, 0)
        self.sink.set_gain(rf_gain, 0)
        self.sink.set_if_gain(if_gain, 0)
        self.sink.set_bb_gain(20, 0)
        self.sink.set_bandwidth(bandwidth, 0)
        
        # Connect
        self.connect(self.source, self.gate, self.sink)
    
    def _calc_gains(self, power):
        """Calculate RF/IF gains from power level"""
        if power <= 5:
            rf_gain = 0
            if_gain = max(0, power + 42)
        else:
            rf_gain = 14
            if_gain = min(47, power + 33)
        return int(rf_gain), int(if_gain)
    
    def retune(self, freq):
        """Fast retune without stopping"""
        self.sink.set_center_freq(freq, 0)
    
    def tx_on(self):
        """Enable transmission (instant)"""
        self.gate.set_k(1.0)
        self.is_transmitting = True
    
    def tx_off(self):
        """Disable transmission (instant)"""
        self.gate.set_k(0.0)
        self.is_transmitting = False


# =============================================================================
# REACTIVE JAMMER CONTROLLER
# =============================================================================

class FastReactiveJammer:
    """
    High-speed reactive jammer controller.
    
    Architecture:
    - RX thread: Rapidly sweeps 5 frequency chunks (~40ms full sweep)
    - TX thread: Waits for detection, instantly jams at target freq
    - Detection queue: Lock-free communication between threads
    """
    
    def __init__(self, config=None):
        self.config = config if config else Config()
        
        # State
        self.running = False
        self.noise_floors = {}  # Per-frequency noise floors
        self.thresholds = {}    # Per-frequency thresholds
        
        # Detection queue (maxlen prevents memory growth)
        self.detection_queue = deque(maxlen=100)
        self.detection_lock = threading.Lock()
        
        # Timing for TX holdoff
        self.last_jam_time = 0
        self.jam_until = 0
        
        # Statistics
        self.stats = {
            'rx_cycles': 0,
            'detections': 0,
            'jam_activations': 0,
            'total_jam_time': 0,
            'last_detection_freq': 0,
        }
        
        # Hardware
        self.rx = None
        self.tx = None
        
        # Threads
        self.rx_thread = None
        self.tx_thread = None
    
    def calibrate(self):
        """
        Fast noise floor calibration.
        Measures each frequency chunk briefly.
        """
        print("\n" + "="*60)
        print("NOISE FLOOR CALIBRATION")
        print("="*60)
        print(">>> ENSURE DRONE IS OFF <<<\n")
        
        # Create temporary RX for calibration
        rx = FastSpectrumMonitor(
            self.config.RX_DEVICE,
            self.config.SAMPLE_RATE,
            self.config.BANDWIDTH,
            self.config.FFT_SIZE
        )
        rx.start()
        time.sleep(0.1)  # Let it stabilize
        
        for freq in self.config.SWEEP_FREQS:
            rx.retune(freq)
            time.sleep(0.02)  # Brief settling
            
            # Collect samples
            samples = []
            for _ in range(self.config.CALIBRATION_SAMPLES):
                power = rx.get_power()
                if power > 0:
                    samples.append(power)
                time.sleep(0.005)
            
            if samples:
                # Use median (robust to outliers)
                noise = np.median(samples)
                noise_db = 10 * np.log10(noise + 1e-12)
                threshold_db = noise_db + self.config.THRESHOLD_MARGIN_DB
                threshold = 10 ** (threshold_db / 10)
                
                self.noise_floors[freq] = noise
                self.thresholds[freq] = threshold
                
                print(f"  {freq/1e6:.0f} MHz: noise={noise_db:.1f}dB, threshold={threshold_db:.1f}dB")
            else:
                # Fallback
                self.noise_floors[freq] = 1e-7
                self.thresholds[freq] = 1e-6
                print(f"  {freq/1e6:.0f} MHz: using defaults (no samples)")
        
        rx.stop()
        rx.wait()
        
        print("\nCalibration complete!")
        print("="*60 + "\n")
        return True
    
    def _rx_loop(self):
        """
        RX thread: Rapid continuous sweep.
        Never stops - just keeps cycling through frequencies.
        """
        print("[RX] Starting rapid sweep loop")
        
        freq_idx = 0
        num_freqs = len(self.config.SWEEP_FREQS)
        
        while self.running:
            # Get current frequency
            freq = self.config.SWEEP_FREQS[freq_idx]
            
            # Retune (fast - no restart)
            self.rx.retune(freq)
            
            # Brief dwell to collect samples
            time.sleep(self.config.RX_DWELL_TIME)
            
            # Get power measurement
            power = self.rx.get_power()
            threshold = self.thresholds.get(freq, 1e-6)
            
            # Check for activity
            if power > threshold:
                # Activity detected! Signal TX thread
                current_time = time.time()
                
                # Only queue if not in TX holdoff period
                if current_time > self.jam_until + self.config.TX_HOLDOFF:
                    with self.detection_lock:
                        self.detection_queue.append((freq, power, current_time))
                    self.stats['detections'] += 1
                    self.stats['last_detection_freq'] = freq
            
            # Move to next frequency
            freq_idx = (freq_idx + 1) % num_freqs
            self.stats['rx_cycles'] += 1
        
        print("[RX] Sweep loop stopped")
    
    def _tx_loop(self):
        """
        TX thread: Waits for detections, instantly jams.
        Pre-warmed flowgraph - only needs retune + gate on.
        """
        print("[TX] Starting jam response loop")
        
        while self.running:
            detection = None
            
            # Check for pending detections
            with self.detection_lock:
                if self.detection_queue:
                    detection = self.detection_queue.popleft()
            
            if detection:
                freq, power, detect_time = detection
                
                # Calculate latency
                latency_ms = (time.time() - detect_time) * 1000
                
                # Retune jammer to target frequency
                self.tx.retune(freq)
                
                # Enable TX (instant - just opens gate)
                self.tx.tx_on()
                jam_start = time.time()
                self.stats['jam_activations'] += 1
                
                print(f"[JAM] {freq/1e6:.0f}MHz | pwr={power:.2e} | lat={latency_ms:.1f}ms")
                
                # Keep jamming for minimum duration
                time.sleep(self.config.TX_JAM_DURATION)
                
                # Check if more detections at same frequency (extend jamming)
                extend_count = 0
                while extend_count < 5:  # Max extensions
                    with self.detection_lock:
                        # Look for same-frequency detections
                        same_freq = [d for d in self.detection_queue 
                                    if abs(d[0] - freq) < 1e6]  # Within 1MHz
                        if same_freq:
                            self.detection_queue.remove(same_freq[0])
                            extend_count += 1
                            time.sleep(self.config.TX_JAM_DURATION)
                        else:
                            break
                
                # Disable TX
                self.tx.tx_off()
                jam_end = time.time()
                
                self.stats['total_jam_time'] += (jam_end - jam_start)
                self.jam_until = jam_end
                
            else:
                # No detection - brief sleep to prevent CPU spin
                time.sleep(0.001)
        
        print("[TX] Jam response loop stopped")
    
    def start(self):
        """Initialize and start the system"""
        print("\n" + "="*60)
        print("FAST REACTIVE JAMMER")
        print("="*60)
        print(f"RX Device: {self.config.RX_DEVICE}")
        print(f"TX Device: {self.config.TX_DEVICE}")
        print(f"Band: {self.config.FREQ_MIN/1e6:.0f}-{self.config.FREQ_MAX/1e6:.0f} MHz")
        print(f"Sweep frequencies: {len(self.config.SWEEP_FREQS)}")
        print(f"RX dwell time: {self.config.RX_DWELL_TIME*1000:.0f}ms")
        print(f"TX power: {self.config.TX_POWER_DBM} dBm")
        print("="*60 + "\n")
        
        # Initialize RX
        print("[INIT] Creating RX flowgraph...")
        self.rx = FastSpectrumMonitor(
            self.config.RX_DEVICE,
            self.config.SAMPLE_RATE,
            self.config.BANDWIDTH,
            self.config.FFT_SIZE
        )
        self.rx.start()
        
        # Initialize TX (pre-warmed, gate closed)
        print("[INIT] Creating TX flowgraph...")
        self.tx = FastJammer(
            self.config.TX_DEVICE,
            self.config.SAMPLE_RATE,
            self.config.BANDWIDTH,
            self.config.TX_POWER_DBM
        )
        self.tx.start()
        
        # Let flowgraphs stabilize
        time.sleep(0.2)
        
        # Start threads
        self.running = True
        
        self.rx_thread = threading.Thread(target=self._rx_loop, daemon=True)
        self.tx_thread = threading.Thread(target=self._tx_loop, daemon=True)
        
        self.rx_thread.start()
        self.tx_thread.start()
        
        print("[SYSTEM] Reactive jammer ACTIVE\n")
    
    def run(self, duration=None):
        """Run for specified duration"""
        if duration is None:
            duration = self.config.TOTAL_DURATION
        
        start_time = time.time()
        last_status = 0
        
        try:
            while time.time() - start_time < duration:
                elapsed = int(time.time() - start_time)
                
                # Status update every 5 seconds
                if elapsed > last_status and elapsed % 5 == 0:
                    last_status = elapsed
                    sweep_rate = self.stats['rx_cycles'] / max(1, elapsed)
                    last_freq = self.stats['last_detection_freq']
                    
                    print(f"[{elapsed}s] sweeps:{self.stats['rx_cycles']} "
                          f"({sweep_rate:.0f}/s) | "
                          f"detect:{self.stats['detections']} | "
                          f"jams:{self.stats['jam_activations']} | "
                          f"last:{last_freq/1e6:.0f}MHz")
                
                time.sleep(0.1)
                
        except KeyboardInterrupt:
            print("\n[SYSTEM] Interrupted by user")
    
    def stop(self):
        """Clean shutdown"""
        print("\n[SYSTEM] Shutting down...")
        
        self.running = False
        
        # Wait for threads
        if self.rx_thread:
            self.rx_thread.join(timeout=1)
        if self.tx_thread:
            self.tx_thread.join(timeout=1)
        
        # Stop flowgraphs
        if self.tx:
            self.tx.tx_off()
            self.tx.stop()
            self.tx.wait()
        
        if self.rx:
            self.rx.stop()
            self.rx.wait()
        
        # Final stats
        print("\n" + "="*60)
        print("SESSION STATISTICS")
        print("="*60)
        print(f"Total RX sweep cycles:  {self.stats['rx_cycles']}")
        print(f"Total detections:       {self.stats['detections']}")
        print(f"Total jam activations:  {self.stats['jam_activations']}")
        print(f"Total jam time:         {self.stats['total_jam_time']:.2f}s")
        if self.stats['detections'] > 0:
            hit_rate = self.stats['jam_activations'] / self.stats['detections'] * 100
            print(f"Detection->Jam rate:    {hit_rate:.1f}%")
        print("="*60)


# =============================================================================
# MAIN
# =============================================================================

jammer = None

def signal_handler(signum, frame):
    global jammer
    if jammer:
        jammer.stop()
    sys.exit(0)


def main():
    global jammer
    
    print("""
    ╔══════════════════════════════════════════════════════════════╗
    ║     FAST REACTIVE JAMMER - DJI 2.4GHz FHSS Hunter            ║
    ║                                                              ║
    ║     Optimized for minimum latency reactive jamming           ║
    ║     FOR CONTROLLED LAB ENVIRONMENT ONLY                      ║
    ╚══════════════════════════════════════════════════════════════╝
    """)
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Parse args
    skip_cal = '--skip-cal' in sys.argv
    
    # Load configuration (from config.yaml if present, otherwise defaults)
    config = load_config()
    
    # Create jammer with loaded config
    jammer = FastReactiveJammer(config)
    
    # Calibration
    if not skip_cal:
        print(">>> Turn OFF the drone for noise floor calibration <<<")
        input("Press Enter when ready...")
        
        if not jammer.calibrate():
            print("Calibration failed!")
            sys.exit(1)
        
        print("\n>>> You can now turn ON the drone <<<")
        input("Press Enter to start hunting...")
    else:
        # Use defaults
        for freq in config.SWEEP_FREQS:
            jammer.noise_floors[freq] = 1e-7
            jammer.thresholds[freq] = 5e-7
        print("[WARNING] Skipping calibration - using default thresholds")
    
    # Start system
    jammer.start()
    
    # Run
    jammer.run()
    
    # Cleanup
    jammer.stop()
    print("\nDone.")


if __name__ == "__main__":
    main()
