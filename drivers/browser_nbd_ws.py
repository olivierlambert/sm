#!/usr/bin/python3
# SPDX-License-Identifier: LGPL-2.1-only
"""Spike: relay a tapdisk Unix byte stream over a binary WebSocket.

No NBD translation, kernel NBD device, ISO cache, or reconnect in mid-stream.
Install websocket-client separately; never disable TLS verification.
"""
import json
import os
import socket
import struct
import sys
import threading

# Isolated dependency for the lab installer; packaged installations use sys.path.
sys.path.insert(0, '/opt/browser-media-prototype/python')
import websocket


def connect(url):
    ws = websocket.create_connection(url, timeout=30, redirect_limit=0,
                                     enable_multithread=True, suppress_origin=True,
                                     http_no_proxy=['*'])
    if ws.getstatus() != 101:
        ws.shutdown()
        raise ValueError('WebSocket upgrade failed')
    return ws


def probe(url, size):
    ws = connect(url)
    try:
        data = b''
        while len(data) < 152:
            chunk = ws.recv()
            if not isinstance(chunk, bytes) or not chunk:
                raise ValueError('Invalid NBD handshake')
            data += chunk
            if len(data) > 152:
                raise ValueError('Invalid NBD handshake length')
        magic, version, actual_size, flags = struct.unpack('!QQQI', data[:28])
        if (magic != 0x4e42444d41474943 or version != 0x420281861253 or
                actual_size != size or flags & 3 != 3):
            raise ValueError('NBD export metadata mismatch')
    finally:
        ws.shutdown()


def relay(client, url):
    ws = None
    try:
        ws = connect(url)
        # Idle media is valid. Active writes remain bounded by socket timeouts;
        # XO owns liveness of the tab and closes all relays on tab loss.
        ws.settimeout(None)
        client.settimeout(35)

        def send_requests():
            try:
                while True:
                    # An idle reader must not expire between guest requests.
                    import select
                    select.select([client], [], [])
                    data = client.recv(65536)
                    if not data:
                        break
                    ws.send_binary(data)
            except Exception:
                pass
            finally:
                ws.shutdown()
                try:
                    client.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

        worker = threading.Thread(target=send_requests, daemon=True)
        worker.start()
        while True:
            data = ws.recv()
            if not isinstance(data, bytes) or not data or len(data) > 1024 * 1024:
                break
            client.sendall(data)
    except Exception:
        # Exceptions may contain the bearer URL: do not print them.
        pass
    finally:
        if ws is not None:
            ws.shutdown()
        try:
            client.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        client.close()


def main():
    with open(sys.argv[2]) as config_file:
        config = json.load(config_file)
    if sys.argv[1] == 'probe':
        probe(config['url'], int(config['size']))
        return
    path = sys.argv[3]
    os.umask(0o077)
    slots = threading.BoundedSemaphore(8)
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(path)
        listener.listen(8)
        while True:
            client, _ = listener.accept()
            if not slots.acquire(False):
                client.close()
                continue

            def run(connection=client):
                try:
                    relay(connection, config['url'])
                finally:
                    slots.release()
            threading.Thread(target=run, daemon=True).start()


if __name__ == '__main__':
    try:
        main()
    except Exception:
        sys.stderr.write('Browser NBD adapter failed\n')
        sys.exit(1)
