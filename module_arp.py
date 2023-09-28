################################################################
# Licensed under the BSD 3-Clause License                      #
# See https://github.com/knokbak/cyber-tools/blob/main/LICENSE #
################################################################

# TX/RX ethernet ARP frames
# OK - 27 Sep 2023

import io
import ipaddress
import socket
import threading
import time
import random

from logging import warn
from functools import partial
from typing import Callable
from utils import confirm_network_transmit, determine_ip_version, generate_random_mac_address, get_interface_mac_address, ipv4_bytes_to_str, ipv6_bytes_to_str, mac_bytes_to_str, mac_str_to_bytes, make_progress_bar, print_table, prompt_menu

def parse_arp_frame(frame: bytes):
    stream = io.BytesIO(frame)

    # Ethernet header
    eth_header = stream.read(14)
    ETH_DEST_MAC = mac_bytes_to_str(eth_header[:6])
    ETH_SRC_MAC = mac_bytes_to_str(eth_header[6:12])
    ETH_TYPE = int.from_bytes(eth_header[12:14], 'big')
        
    # ARP header
    arp_header = stream.read(8)
    ARP_HTYPE = int.from_bytes(arp_header[0:2], 'big')
    ARP_PTYPE = int.from_bytes(arp_header[2:4], 'big')
    ARP_HLEN = arp_header[4]
    ARP_PLEN = arp_header[5]
    ARP_OPER = int.from_bytes(arp_header[6:8], 'big')

    if ARP_HTYPE != 1:
        warn(f'ARP_HTYPE is not 1, ignoring as this is not an Ethernet request')
        raise ValueError

    if ARP_HLEN != 6:
        warn(f'ARP_HLEN and ARP_HTYPE mismatch; ARP_HLEN = {ARP_HLEN}, ARP_HTYPE = {ARP_HTYPE}, ignoring frame')
        raise ValueError

    match ARP_PTYPE:
        case 0x0800:
            IP_PRO_VER = 4
            IP_ADDR_LEN = 4
        case 0x86DD:
            IP_PRO_VER = 6
            IP_ADDR_LEN = 16
        case _:
            warn(f'ARP_PTYPE is not 0x0800 or 0x86DD, ignoring as this is not an IPv4 or IPv6 request')
            raise ValueError

    if ARP_PLEN != IP_ADDR_LEN:
        warn(f'ARP_PLEN and ARP_PTYPE mismatch; ARP_PLEN = {ARP_PLEN}, ARP_PTYPE = {ARP_PTYPE}, ignoring frame')
        raise ValueError

    # Sender addresses
    arp_sender_addresses = stream.read(6 + IP_ADDR_LEN)
    ARP_SHA_MAC_ADDR = mac_bytes_to_str(arp_sender_addresses[:6])

    # Target addresses
    arp_target_addresses = stream.read(6 + IP_ADDR_LEN)
    ARP_THA_MAC_ADDR = mac_bytes_to_str(arp_target_addresses[:6])

    match IP_PRO_VER:
        case 4:
            ARP_SPA_PRO_ADDR = ipv4_bytes_to_str(arp_sender_addresses[6:10])
            ARP_TPA_PRO_ADDR = ipv4_bytes_to_str(arp_target_addresses[6:10])
        case 6:
            ARP_SPA_PRO_ADDR = ipv6_bytes_to_str(arp_sender_addresses[6:22])
            ARP_TPA_PRO_ADDR = ipv6_bytes_to_str(arp_target_addresses[6:22])
    
    return ETH_DEST_MAC, ETH_SRC_MAC, ETH_TYPE, ARP_HTYPE, ARP_PTYPE, ARP_HLEN, ARP_PLEN, ARP_OPER, IP_PRO_VER, IP_ADDR_LEN, ARP_SHA_MAC_ADDR, ARP_SPA_PRO_ADDR, ARP_THA_MAC_ADDR, ARP_TPA_PRO_ADDR


def monitor(interface: str):
    sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(0x0806))
    sock.bind((interface, 0))
    print('Listening for ARP frames...')
    print('Running in silent mode, no frames will be transmitted. We are sniffing!')
    seen_hosts = {}
    start_time = time.time()
    try:
        while True:
            frame = sock.recv(65535)
            try:
                ETH_DEST_MAC, ETH_SRC_MAC, ETH_TYPE, ARP_HTYPE, ARP_PTYPE, ARP_HLEN, ARP_PLEN, ARP_OPER, IP_PRO_VER, IP_ADDR_LEN, ARP_SHA_MAC_ADDR, ARP_SPA_PRO_ADDR, ARP_THA_MAC_ADDR, ARP_TPA_PRO_ADDR = parse_arp_frame(frame)
            except ValueError:
                continue

            match ARP_OPER:
                case 1:
                    print(f'[REQUEST] {ETH_SRC_MAC} -> {ETH_DEST_MAC} : who is {ARP_TPA_PRO_ADDR} (IPv{IP_PRO_VER})?')
                case 2:
                    print(f'[REPLY]   {ETH_SRC_MAC} -> {ETH_DEST_MAC} : {ARP_SPA_PRO_ADDR} is {ARP_SHA_MAC_ADDR} (IPv{IP_PRO_VER})')
                    if ARP_SPA_PRO_ADDR in seen_hosts:
                        seen_hosts[ARP_SPA_PRO_ADDR] = {
                            'mac': ARP_SHA_MAC_ADDR,
                            'count': seen_hosts[ARP_SPA_PRO_ADDR]['count'] + 1,
                            'last_seen': time.time()
                        }
                    else:
                        seen_hosts[ARP_SPA_PRO_ADDR] = {
                            'mac': ARP_SHA_MAC_ADDR,
                            'count': 1,
                            'last_seen': time.time()
                        }
                case _:
                    continue
    except KeyboardInterrupt:
        sock.close()
        print(f'\n\nARP sniffing stopped. Seen {len(seen_hosts)} hosts in {round(time.time() - start_time)} seconds.\n')
        table_list = [ [ 'IP Address', 'MAC Address', 'Count', 'Last Seen' ] ]
        for host in seen_hosts:
            table_list.append([ host, seen_hosts[host]['mac'], seen_hosts[host]['count'], f'{round(time.time() - seen_hosts[host]["last_seen"])}s ago' ])
        print_table(table_list)
        raise KeyboardInterrupt


# Build and transmit an ARP request
def transmit_arp_request_ipv4(interface: str, target_ip: str, source_mac: str, source_ip: str):
    sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(0x0806))
    sock.bind((interface, 0))

    # Ethernet header
    eth_header = mac_str_to_bytes('ff:ff:ff:ff:ff:ff') + mac_str_to_bytes(source_mac) + bytes.fromhex('0806')

    # ARP header
    HTYPE = bytes.fromhex('0001') # Using Ethernet
    PTYPE = bytes.fromhex('0800') # Using IPv4
    HLEN = bytes.fromhex('06') # MAC address length
    PLEN = bytes.fromhex('04') # IPv4 address length
    OPER = bytes.fromhex('0001') # We are sending a resolution request
    arp_header = HTYPE + PTYPE + HLEN + PLEN + OPER

    # Sender addresses
    SHA = mac_str_to_bytes(source_mac) # Our MAC address
    SPA = ipaddress.IPv4Address(source_ip).packed # Our IPv4 address

    # Target addresses
    THA = bytes.fromhex('000000000000') # We don't know the target's MAC address
    TPA = ipaddress.IPv4Address(target_ip).packed # The target's IPv4 address

    arp_payload = SHA + SPA + THA + TPA
    frame = eth_header + arp_header + arp_payload
    sock.send(frame)
    sock.close()


def listen_for_arp_reply_ipv4(interface: str, target_mac: str):
    responses: list[tuple[str, str]] = []
    should_stop = threading.Event()

    def thread():
        sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(0x0806))
        sock.bind((interface, 0))
        while True:
            if should_stop.is_set():
                return

            try:
                sock.setblocking(False)
                frame = sock.recv(65535)
            except BlockingIOError:
                continue

            try:
                ETH_DEST_MAC, ETH_SRC_MAC, ETH_TYPE, ARP_HTYPE, ARP_PTYPE, ARP_HLEN, ARP_PLEN, ARP_OPER, IP_PRO_VER, IP_ADDR_LEN, ARP_SHA_MAC_ADDR, ARP_SPA_PRO_ADDR, ARP_THA_MAC_ADDR, ARP_TPA_PRO_ADDR = parse_arp_frame(frame)
            except ValueError:
                continue

            if ARP_OPER != 2:
                continue
                
            if ETH_DEST_MAC != target_mac:
                continue

            print(f'{ETH_SRC_MAC} -> {ETH_DEST_MAC} : {ARP_SPA_PRO_ADDR} is {ARP_SHA_MAC_ADDR} (IPv{IP_PRO_VER})')
            responses.append((ARP_SPA_PRO_ADDR, ARP_SHA_MAC_ADDR))


    thr = threading.Thread(target=thread, daemon=True)
    thr.start()

    def stop():
        should_stop.set()
        thr.join()
        return responses

    return thr, stop


def probe(interface: str):
    range = input('Enter a range of hosts to probe or an IP address: ')

    try:
        ip_version = determine_ip_version(range.split('/')[0])
    except ValueError:
        print('Invalid IP address')
        return probe(interface)
    
    match ip_version:
        case 4:
            network = ipaddress.IPv4Network(range)
        case 6:
            network = ipaddress.IPv6Network(range)
        case _:
            raise ValueError('Invalid IP version')
    
    default_mac_address = get_interface_mac_address(interface)
    tx_mac_address = input(f'Enter a MAC address to transmit from (or "random" to create one) [{default_mac_address}]: ').lower() or default_mac_address

    if tx_mac_address == 'random':
        tx_mac_address = generate_random_mac_address()
        print(F'Using a random MAC address: {tx_mac_address}')
        
    
    should_randomize = (input('Should I shuffle the hosts in this network? [Y/n]: ').lower() or 'y') == 'y'

    hosts = list(network.hosts())
    if should_randomize:
        random.shuffle(hosts)

    wait_time = float(input('Enter a wait time between each probe in seconds [0.01]: ') or '0.01')
    timeout = int(input('Enter a timeout in seconds [5]: ') or '5')
    
    if not confirm_network_transmit():
        return main()
    
    start_time = time.time()
    listen_thr, listen_stop = listen_for_arp_reply_ipv4(interface, tx_mac_address)

    try:
        print(f'\nARP probing has begun. Sending requests to {len(hosts)} hosts...\nThis may take a while. Press Ctrl+C to stop.\n')

        if len(hosts) > 1:
            print(make_progress_bar(f'Transmitting: 0 / {len(hosts)}', 0, len(hosts)), end='\r')

        for host in hosts:
            transmit_arp_request_ipv4(interface, str(host), tx_mac_address, '0.0.0.0')
            if len(hosts) > 1:
                print(make_progress_bar(f'Transmitting: {host} ({hosts.index(host) + 1} / {len(hosts)})      ', hosts.index(host) + 1, len(hosts)), end='\r')
                time.sleep(wait_time)
        
        if len(hosts) > 1:
            print('\nWaiting for responses...\n')

        time.sleep(timeout)
        responses = listen_stop()

        print(f'\nSeen {len(responses)} hosts in {round(time.time() - start_time)} seconds.\n')
        table_list = [ [ 'IP Address', 'MAC Address' ] ]
        for response in responses:
            table_list.append([ response[0], response[1] ])
        print_table(table_list)
        print()
    except KeyboardInterrupt:
        listen_stop()
        print('\n\nARP probing was interrupted.\n')
        raise KeyboardInterrupt


def main(interface = None):
    if not interface:
        interface = input('Enter an interface [eth0]: ').lower() or 'eth0'

    try:
        prompt_menu('ARP Menu', [
            ('Listen for frames - do not TX', partial(monitor, interface)),
            ('Probe host or range', partial(probe, interface)),
        ])
    except KeyboardInterrupt:
        main(interface)
