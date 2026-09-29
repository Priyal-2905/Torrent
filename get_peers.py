import urllib.parse
import urllib.request
import hashlib
import os
import random
from parser import bdecode, bencode

def get_peers_from_tracker(torrent_file_path, port=6881, numwant=50):
    with open(torrent_file_path, 'rb') as f:
        torrent_data = f.read()

    decoded = bdecode(torrent_data)

    if b'announce' not in decoded:
        raise ValueError("Torrent file missing 'announce' key")

    announce_url = decoded[b'announce'].decode('utf-8')

    info = decoded[b'info']
    info_bencoded = bencode(info)
    info_hash = hashlib.sha1(info_bencoded).digest()

    if b'length' in info:
        left = info[b'length']
    elif b'files' in info:
        left = sum(f[b'length'] for f in info[b'files'])
    else:
        raise ValueError("Invalid info dictionary: missing 'length' or 'files'")

    peer_id_prefix = b'-PY0001-'
    peer_id = peer_id_prefix + os.urandom(20 - len(peer_id_prefix))

    params = {
        'info_hash': info_hash,
        'peer_id': peer_id,
        'port': port,
        'uploaded': 0,
        'downloaded': 0,
        'left': left,
        'compact': 1,
        'event': 'started',
        'numwant': numwant,
    }

    query_parts = []

    for key, val in params.items():
        if isinstance(val, bytes):
            encoded_val = urllib.parse.quote(val, safe='')
            query_parts.append(f"{key}={encoded_val}")
        else:
            query_parts.append(
                f"{key}={urllib.parse.quote(str(val), safe='')}"
            )

    query_string = '&'.join(query_parts)

    if '?' in announce_url:
        full_url = announce_url + '&' + query_string
    else:
        full_url = announce_url + '?' + query_string

    print(f"Announce URL: {announce_url}")
    print(f"Full announce URL: {full_url}")

    req = urllib.request.Request(full_url)
    req.add_header('User-Agent', 'Python-BitTorrent-Client/1.0')

    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            tracker_data = response.read()
    except urllib.error.HTTPError as e:
        error_body = e.read().decode('utf-8', errors='ignore')
        raise ValueError(
            f"Tracker returned error: {e.code} - {error_body}"
        )

    print(f"Received tracker response: {tracker_data}")

    tracker_decoded = bdecode(tracker_data)

    if b'failure reason' in tracker_decoded:
        failure = tracker_decoded[b'failure reason'].decode('utf-8')
        raise ValueError(f"Tracker failure: {failure}")

    if b'peers' not in tracker_decoded:
        raise ValueError("Tracker response missing 'peers' key")

    peers_data = tracker_decoded[b'peers']

    peers = []

    if isinstance(peers_data, bytes):
        if len(peers_data) % 6 != 0:
            raise ValueError("Invalid compact peers format")

        for i in range(0, len(peers_data), 6):
            ip_bytes = peers_data[i:i+4]
            port_bytes = peers_data[i+4:i+6]

            ip = '.'.join(map(str, ip_bytes))
            port = int.from_bytes(port_bytes, 'big')

            peers.append((ip, port))

    elif isinstance(peers_data, list):
        for peer_dict in peers_data:
            ip = peer_dict[b'ip'].decode('utf-8')
            port = peer_dict[b'port']
            peers.append((ip, port))

    else:
        raise ValueError("Unknown peers format")

    return peers


if __name__ == "__main__":
    try:
        peers = get_peers_from_tracker('one-piece.torrent')

        print(f"\nFound {len(peers)} peers from tracker:")

        for ip, port in peers[:10]:
            print(f"  {ip}:{port}")

        if len(peers) > 10:
            print(f"  ... and {len(peers) - 10} more")

    except Exception as e:
        print(f"Error: {e}")

        import traceback
        traceback.print_exc()