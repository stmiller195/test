import argparse
import logging
import os
import readline
import signal
import socket
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from ssl import SSLError
from threading import Event
from time import sleep
from urllib.error import HTTPError, URLError

from nuxhash import settings, utils
from nuxhash.devices.nvidia import enumerate_devices as nvidia_devices
from nuxhash.devices.nvidia import NvidiaDevice
from nuxhash.download.downloads import make_miners
from nuxhash.miners.excavator import Excavator
from nuxhash.miners.miner import MinerNotRunning
from nuxhash.nicehash import simplemultialgo_info
from nuxhash.switching.naive import NaiveSwitcher

BENCHMARK_SECS = 90

def main():
    # parse commmand-line arguments
    argp = argparse.ArgumentParser(description='Sell GPU hash power on the NiceHash market.')
    argp_benchmark = argp.add_mutually_exclusive_group()
    argp_benchmark.add_argument('--benchmark-all', action='store_true',
                                help='benchmark all algorithms on all devices')
    argp_benchmark.add_argument('--benchmark-missing', action='store_true',
                                help='benchmark algorithm-device combinations not measured')
    argp.add_argument('--list-devices', action='store_true',
                      help='list all devices')
    argp.add_argument('-v', '--verbose', action='store_true',
                      help='print more information to the console log')
    argp.add_argument('--show-mining', action='store_true',
                      help='print output from mining processes, implies --verbose')
    argp.add_argument('-c', '--configdir', nargs=1, default=[settings.DEFAULT_CONFIGDIR],
                      help='directory for configuration and benchmark files (default: ~/.config/nuxhash/)')
    args = argp.parse_args()
    config_dir = Path(args.configdir[0])

    # initiate logging
    if args.benchmark_all:
        log_level = logging.ERROR
    elif args.show_mining:
        log_level = logging.DEBUG
    elif args.verbose:
        log_level = logging.INFO
    else:
        log_level = logging.WARN
    logging.basicConfig(format='%(asctime)s %(levelname)s: %(message)s', level=log_level)

    # probe graphics cards
    all_devices = nvidia_devices()

    # load from config directory
    nx_settings, nx_benchmarks = settings.load_persistent_data(config_dir, all_devices)

    # if no wallet configured, do initial setup prompts
    if nx_settings['nicehash']['wallet'] == '':
        wallet, workername, region = initial_setup()
        nx_settings['nicehash']['wallet'] = wallet
        nx_settings['nicehash']['workername'] = workername
        nx_settings['nicehash']['region'] = region

    # download and initialize miners
    downloadables = make_miners(config_dir)
    for d in downloadables:
        if not d.verify():
            logging.info('Downloading %s' % d.name)
            d.download()
    nx_miners = [Excavator(config_dir, nx_settings)]

    if args.benchmark_all:
        nx_benchmarks = run_missing_benchmarks(nx_miners, nx_settings, all_devices,
                                               defaultdict(lambda: {}))
    elif args.benchmark_missing:
        nx_benchmarks = run_missing_benchmarks(nx_miners, nx_settings, all_devices,
                                               nx_benchmarks)
    elif args.list_devices:
        list_devices(all_devices)
    else:
        do_mining(nx_miners, nx_settings, nx_benchmarks, all_devices)

    # save to config directory
    settings.save_persistent_data(config_dir, nx_settings, nx_benchmarks)

def initial_setup():
    print('nuxhashd initial setup')

    wallet = input('Wallet address: ')
    workername = input('Worker name: ')
    region = input('Region (eu/usa/hk/jp/in/br): ')

    print()

    return wallet, workername, region

def run_missing_benchmarks(miners, settings, devices, old_benchmarks):
    stratums = simplemultialgo_info(settings)[1]

    algorithms = sum([miner.algorithms for miner in miners], [])
    def algorithm(name): return next((a for a in algorithms if a.name == name), None)
    for miner in miners:
        miner.stratums = stratums
        miner.load()

    done = sum([[(device, algorithm(algorithm_name)) for algorithm_name in benchmarks.keys()]
                for device, benchmarks in old_benchmarks.items()], [])
    all_targets = sum([[(device, algorithm) for algorithm in algorithms]
                       for device in devices], [])
    benchmarks = run_benchmarks(set(all_targets) - set(done))

    for miner in miners:
        miner.unload()

    for d in benchmarks:
        old_benchmarks[d].update(benchmarks[d])
    return old_benchmarks

def run_benchmarks(targets):
    if len(targets) == 0:
        print('Nothing to benchmark.')
        return []

    benchmarks = defaultdict(lambda: {})
    last_device = None
    for device, algorithm in sorted(targets, key=lambda t: str(t[0])):
        if device != last_device:
            if isinstance(device, NvidiaDevice):
                print('\nCUDA device: %s (%s)' % (device.name, device.uuid))
            last_device = device
        try:
            benchmarks[device][algorithm.name] = run_benchmark(device, algorithm)
        except MinerNotRunning:
            print('  %s: failed to complete benchmark     ' % algorithm.name)
            benchmarks[device][algorithm.name] = [0]*len(algorithm.algorithms)
        except KeyboardInterrupt:
            print('Benchmarking aborted (completed benchmarks saved).')
            break
    return benchmarks

def run_benchmark(device, algorithm):
    status_dot = [-1]
    def report_speeds(sample, secs_remaining):
        status_dot[0] = (status_dot[0] + 1) % 3
        status_line = ''.join(['.' if i == status_dot[0] else ' ' for i in range(3)])
        if secs_remaining < 0:
            print('  %s %s %s (warming up, %s)\r' %
                  (algorithm.name, status_line, utils.format_speeds(sample),
                   format_time(-secs_remaining)), end='')
        else:
            print('  %s %s %s (sampling, %s)  \r' %
                  (algorithm.name, status_line, utils.format_speeds(sample),
                   utils.format_time(secs_remaining)), end='')
        sys.stdout.flush()
    speeds = utils.run_benchmark(algorithm, device,
                                 algorithm.warmup_secs, BENCHMARK_SECS,
                                 sample_callback=report_speeds)
    print('  %s: %s                      ' % (algorithm.name,
                                              utils.format_speeds(speeds)))
    return speeds

def list_devices(nx_devices):
    for d in sorted(nx_devices, key=str):
        if isinstance(d, NvidiaDevice):
            print('CUDA device: %s (%s)' % (d.name, d.uuid))

def do_mining(nx_miners, nx_settings, nx_benchmarks, nx_devices):
    # get algorithm -> port information for stratum URLs
    logging.info('Querying NiceHash for miner connection information...')
    mbtc_per_hash = download_time = stratums = None
    while mbtc_per_hash is None:
        try:
            mbtc_per_hash, stratums = simplemultialgo_info(nx_settings)
        except (HTTPError, URLError, socket.error, socket.timeout):
            pass
        else:
            download_time = datetime.now()

    # initialize miners
    for miner in nx_miners:
        miner.stratums = stratums
    algorithms = sum([miner.algorithms for miner in nx_miners], [])

    # initialize profit-switching
    profit_switch = NaiveSwitcher(nx_settings)
    profit_switch.reset()

    # quit signal
    quit = Event()
    signal.signal(signal.SIGINT, lambda signum, frame: quit.set())

    current_algorithm = {d: None for d in nx_devices}
    revenues = {d: defaultdict(lambda: 0.0) for d in nx_devices}
    while not quit.is_set():
        # calculate profitability per algorithm per device
        def mbtc_per_day(device, algorithm):
            device_benchmarks = nx_benchmarks[device]
            if algorithm.name in device_benchmarks:
                mbtc_per_day_multi = [device_benchmarks[algorithm.name][i]*
                                      mbtc_per_hash[algorithm.algorithms[i]]*(24*60*60)
                                      for i in range(len(algorithm.algorithms))]
                return sum(mbtc_per_day_multi)
            else:
                return 0
        revenues = {device: {algorithm: mbtc_per_day(device, algorithm)
                             for algorithm in algorithms} for device in nx_devices}

        # get algorithm assignments from the profit switcher
        current_algorithm = profit_switch.decide(revenues, download_time)

        # attach devices to respective algorithms atomically
        for algorithm in algorithms:
            my_devices = [d for d, a in current_algorithm.items() if a == algorithm]
            algorithm.set_devices(my_devices)

        # wait for specified interval
        quit.wait(nx_settings['switching']['interval'])

        # probe miner status
        for algorithm in current_algorithm.values():
            if not algorithm.parent.is_running():
                logging.error('Detected %s crash, restarting miner' % algorithm.name)
                algorithm.parent.reload()

        # query nicehash profitability data again
        try:
            mbtc_per_hash = simplemultialgo_info(nx_settings)[0]
        except URLError as err:
            logging.warning('Failed to retrieve NiceHash profitability stats: %s' %
                            err.reason)
        except HTTPError as err:
            logging.warning('Failed to retrieve NiceHash profitability stats: %s %s' %
                            (err.code, err.reason))
        except (socket.timeout, SSLError):
            logging.warning('Failed to retrieve NiceHash profitability stats: timed out')
        except (ValueError, KeyError):
            logging.warning('Failed to retrieve NiceHash profitability stats: bad response')
        else:
            download_time = datetime.now()

    logging.info('Cleaning up')
    for miner in nx_miners:
        miner.unload()
