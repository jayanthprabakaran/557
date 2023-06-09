#!/usr/bin/python

"""Main driver of the experiment to replicate BBR Figure 8.

This code accepts experimental parameters as commandline arguments:
    - RTT
    - Loss Rates to try as [min, max, interval]
    - Link Bandwidth
    - Length of the trace
where the default values are the values used in the BBR paper. Then, we
run the experiments using hte specified parameters, log the results, and
create the corresponding figures.
"""

import argparse
from bbr_logging import debug_print, debug_print_verbose, debug_print_error, stdout_print
from multiprocessing import Process, Queue, Event
import os
from server import Server
import subprocess
import sys
import time


EXIT_SUCCESS = 0


class Flags(object):
    """Dictionary object to store parsed flags."""

    TIME = "time"
    LOSS = "loss"
    PORT = "port"
    CC = "congestion_control"
    RTT = "rtt"
    BW = "bottleneck_bandwidth"
    SIZE = "packet_size"
    TUP = "trace_uplink"
    TDOWN = "trace_downlink"
    HEADLESS = "headless"
    OUTPUT_FILE = "output_file"
    parsed_args = None


def _check_cc(input):
    if input.lower() in ['bbr', 'cubic', 'bic', 'vegas', 'westwood', 'reno', 'bbr557', 'gargbage']:
        return input.lower()
    else:
        raise argparse.ArgumentTypeError(
            "%s is not a supported algorithm" % input)


def _clean_up_trace(throughput):
    for filename in [str(throughput) + str(x) for x in ["Mbps.up", "Mbps.down"]]:
        os.remove(filename)


def _generate_trace(seconds, throughput):
    """Generate a <throughput>Mbps trace that lasts for the specified seconds."""
    debug_print("Creating " + str(seconds) +
                " sec trace @: " + str(throughput) + "Mbps")
    bits_per_packet = 12000
    low_avg = int((throughput) / (bits_per_packet / 1000))
    high_avg = low_avg + 1
    low_err = throughput - (low_avg * 12)
    high_err = throughput - (high_avg * 12)

    for filename in [str(throughput) + str(x) for x in ["Mbps.up", "Mbps.down"]]:
        with open(filename, 'w') as outfile:
            accumulated_err = 0
            num_packets = 0

            for ms_counter in range(int(seconds * 1000)):
                if accumulated_err >= abs(high_err):
                    num_packets = high_avg
                    accumulated_err += high_err
                else:
                    num_packets = low_avg
                    accumulated_err += low_err

                for j in range(num_packets):
                    outfile.write(str(ms_counter + 1) + '\n')


def _parse_args():
    """Parse experimental parameters from the commandline."""
    parser = argparse.ArgumentParser(
        description="Process experimental params.")
    parser.add_argument('--time', dest=Flags.TIME, type=int,
                        help="Enter a time in seconds to run each trace.",
                        default=60)
    parser.add_argument('--loss', dest=Flags.LOSS, type=float,
                        help="Loss rate to test (%%).",
                        default=0.1)
    parser.add_argument('--port', dest=Flags.PORT, type=int,
                        help="Which port to use.",
                        default=5050)
    parser.add_argument('--cc', dest=Flags.CC, type=_check_cc,
                        help="Which congestion control algorithm to compare.",
                        default="cubic")
    parser.add_argument('--output_file', dest=Flags.OUTPUT_FILE, type=str,
                        help="If non empty, will append measurement result to this file.",
                        default="")
    parser.add_argument('--rtt', dest=Flags.RTT, type=int,
                        help="Specify the RTT of the link in milliseconds.",
                        default=100)
    parser.add_argument('--bw', dest=Flags.BW, type=float,
                        help="Specify the bottleneck bandwidth in Mbps.",
                        default=100)
    parser.add_argument('--size', dest=Flags.SIZE, type=int,
                        help="Specify the packet size in bytes.",
                        default=1024)
    parser.add_argument('--traceup', dest=Flags.TUP, type=str,
                        help="Specify the uplink tracefile.",
                        default=None)
    parser.add_argument('--tracedown', dest=Flags.TDOWN, type=str,
                        help="Specify the downlink tracefile.",
                        default=None)
    parser.add_argument('--headless', dest=Flags.HEADLESS, action='store_true',
                        help="Specify whether the Mahimahi Throughput / Queueing delay graphs come up. On Clouds VMs, you'd want to set this to true.",
                        default=False)

    Flags.parsed_args = vars(parser.parse_args())
    # Preprocess the loss into a percentage
    Flags.parsed_args[Flags.LOSS] = Flags.parsed_args[Flags.LOSS] / 100.0
    debug_print_verbose("Parse: " + str(Flags.parsed_args))


def _parse_mahimahi_log(cc):
    # Piped to /dev/null because stdout is just the SVG generated.
    # We just want the throutput information, which is stderr.
    debug_print_verbose("Parsing Mahimahi logs...")
    command_text = "mm-throughput-graph 10 /tmp/mahimahi_log > ~/557/mahimahi/temp/%s_output.svg" % cc
    command = (command_text)
    output = subprocess.check_output(
        command, shell=True, stderr=subprocess.STDOUT)
    output = output.split('\n')
    debug_print_verbose(output)
    capacity = float(output[0].split(' ')[2])
    goodput = float(output[1].split(' ')[2])
    q_delay = float(output[2].split(' ')[5])
    s_delay = float(output[3].split(' ')[4])
    return (capacity, goodput, q_delay, s_delay)


def _is_server_listening(port):
    """Determine whether a server at the given port is listening."""
    command = ["netstat", "-tln", "|", "grep", ":" + str(port)]
    result = subprocess.check_output(command)
    result = result.strip()
    if len(result) > 0:
        # Non empty output means found a listening server.
        return True
    else:
        return False


def _wait_for_server_start(port):
    """Wait until server at given port is running / listening for connections."""
    while(not _is_server_listening(port)):
        debug_print_verbose("Waiting for server start at port %d" % port)
        time.sleep(2)
    debug_print_verbose("Server started listening at port %d" % port)


def _run_experiment(loss, port, cong_ctrl, rtt, throughput, trace_up=None, trace_down=None):
    """Run a single throughput experiment with the given loss rate."""
    debug_print("Running experiment [loss = " +
                str(loss) + ", cong_ctrl = " + str(cong_ctrl) + ", rtt = " + str(rtt) + ", bw = " + str(throughput) + "]")

    client_args = "(\'" + str(cong_ctrl) + "\')"

    headless = Flags.parsed_args[Flags.HEADLESS]

    # We are using an infinite buffer size.
    if not headless:
        if trace_up and trace_down:
            command = ["stdbuf", "-o0", "mm-delay", str(rtt / 2), "mm-loss", "uplink", str(loss),
                       "mm-link", str(trace_up), str(trace_down), "--uplink-log=/tmp/mahimahi_log", "--meter-uplink", "--once"]
        else:
            command = ["stdbuf", "-o0", "mm-delay", str(rtt / 2), "mm-loss", "uplink", str(loss),
                       "mm-link", str(throughput) + "Mbps.up", str(throughput) +
                       "Mbps.down", "--uplink-log=/tmp/mahimahi_log", "--meter-uplink", "--once"]
    else:
        if trace_up and trace_down:
            command = ["stdbuf", "-o0", "mm-delay", str(rtt / 2), "mm-loss", "uplink", str(loss),
                       "mm-link", str(trace_up), str(trace_down), "--uplink-log=/tmp/mahimahi_log", "--once"]
        else:
            command = ["stdbuf", "-o0", "mm-delay", str(rtt / 2), "mm-loss", "uplink", str(loss),
                       "mm-link", str(throughput) + "Mbps.up", str(throughput) +
                       "Mbps.down", "--uplink-log=/tmp/mahimahi_log", "--once"]

    subcommand = ["--", "python", "-c",
                  "from client import run_client; run_client" + client_args]
    full_command = command + subcommand
    debug_print_verbose(str(command) + " " + str(subcommand))
    try:
        subprocess.check_call(full_command, stderr=subprocess.STDOUT)
    except Exception as e:
        debug_print_error("Subprocess call error: " + str(e))
        sys.exit(-1)


def main():
    """Run the experiments."""
    # Grab the experimental parameterss
    _parse_args()

    port = Flags.parsed_args[Flags.PORT]
    size = Flags.parsed_args[Flags.SIZE]
    loss = Flags.parsed_args[Flags.LOSS]
    rtt = Flags.parsed_args[Flags.RTT]
    bw = Flags.parsed_args[Flags.BW]
    cc = Flags.parsed_args[Flags.CC]
    output_file = Flags.parsed_args[Flags.OUTPUT_FILE]
    uplink_trace = Flags.parsed_args[Flags.TUP]
    downlink_trace = Flags.parsed_args[Flags.TDOWN]
    # Generate the trace files based on the parameter
    if uplink_trace is None and downlink_trace is None:
        _generate_trace(Flags.parsed_args[Flags.TIME], bw)

    # Start the client and server
    server_q = Queue()
    e = Event()
    server_proc = Server(server_q, e, cc, port, size)

    # Start client and wait for it to finish.
    if uplink_trace is None and downlink_trace is None:
        client_proc = Process(target=_run_experiment,
                              args=(loss, port, cc, rtt, bw))
    else:
        client_proc = Process(target=_run_experiment,
                              args=(loss, port, cc, rtt, bw, uplink_trace, downlink_trace))

    server_proc.start()
    # Wait a little to give server time to start up.
    time.sleep(2)
    _wait_for_server_start(port)
    client_proc.start()
    client_proc.join()
    # Handle errors starting up the server.
    if not server_proc.is_alive():
        if server_proc.exitcode != EXIT_SUCCESS:
            debug_print_error("Server Process Died unexpectedly. Terminating.")
            sys.exit(-1)

    # Server is still alive, signal it to shutdown.
    debug_print_verbose("Signal server to shutdown.")
    e.set()

    debug_print_verbose("Is Server Alive? %s" % (server_proc.is_alive()))
    # Wait for server to shutdown, upto some timeout.
    server_proc.join(10)
    # Check for errors from the server
    debug_print_verbose("Run complete.")
    while(not server_q.empty()):
        result, exception = server_q.get()
        if exception:
            raise exception
        debug_print_verbose(result)

    server_q.close()

    e.clear()
    (capacity, goodput, q_delay, s_delay) = _parse_mahimahi_log(cc)
    debug_print("Experiment complete!")

    # Print the output
    results = ', '.join([str(x)
                         for x in [cc, loss, goodput, rtt, capacity, bw]])
    stdout_print(results + "\n")

    # Also write to output file if it's set.
    if output_file:
        debug_print_verbose("Appending Result output to: %s" % output_file)
        if os.path.exists(output_file):
            with open(output_file, 'a') as output:
                output.write(results + "\n")
        else:
            with open(output_file, 'a') as output:
                header_line = "congestion_control, loss_rate, goodput_Mbps, rtt_ms, bandwidth_Mbps, specified_bw_Mbps"
                output.write(header_line + "\n")
                output.write(results + "\n")

    if uplink_trace is None and downlink_trace is None:
        _clean_up_trace(bw)

    debug_print("Terminating driver.")


if __name__ == '__main__':
    main()
