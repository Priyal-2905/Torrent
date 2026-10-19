import asyncio
import os
import socket
import struct
import hashlib
import time
import random
from parser import bdecode, bencode
from collections import defaultdict

class AsyncBitTorrentPeer:
    
    def __init__(self, ip, port, info_hash, peer_id, timeout=10):
        self.ip = ip
        self.port = port
        self.info_hash = info_hash
        self.peer_id = peer_id
        self.timeout = timeout
        self.reader = None
        self.writer = None
        self.choked = True
        self.interested = False
        self.peer_choking = True
        self.peer_interested = False
        self.bitfield = None
        self.have_pieces = set()
        self.has_all = False
        self.connected = False
        
    async def connect(self):
        """Establish TCP connection to peer."""
        try:
            self.reader, self.writer = await asyncio.wait_for(
                asyncio.open_connection(self.ip, self.port),
                timeout=self.timeout
            )
            self.connected = True
            return True
        except Exception as e:
            self.connected = False
            return False
    
    async def handshake(self):
        """Perform BitTorrent handshake."""
        pstr = b"BitTorrent protocol"
        pstrlen = len(pstr)
        reserved = b'\x00' * 8
        
        handshake_msg = struct.pack("B", pstrlen) + pstr + reserved + self.info_hash + self.peer_id
        
        try:
            self.writer.write(handshake_msg)
            await self.writer.drain()
            
            # Receive handshake response (68 bytes total)
            response = await asyncio.wait_for(
                self.reader.readexactly(68),
                timeout=self.timeout
            )
            
            # Parse response
            resp_pstrlen = response[0]
            resp_pstr = response[1:20]
            resp_info_hash = response[28:48]
            
            # Verify the response
            if resp_pstrlen != 19 or resp_pstr != pstr:
                return False
            
            if resp_info_hash != self.info_hash:
                return False
            
            return True
            
        except Exception:
            self.connected = False
            return False
    
    async def send_interested(self):
        """Send 'interested' message to peer."""
        if not self.connected or not self.writer:
            return
        try:
            msg = struct.pack(">IB", 1, 2)
            self.writer.write(msg)
            await self.writer.drain()
            self.interested = True
        except Exception:
            self.connected = False
    
    async def send_request(self, piece_index, begin, length):
        """Request a block from peer."""
        if not self.connected or not self.writer:
            return
        try:
            msg = struct.pack(">IBIII", 13, 6, piece_index, begin, length)
            self.writer.write(msg)
            await self.writer.drain()
        except Exception:
            self.connected = False

    async def send_cancel(self, piece_index, begin, length):
        """Cancel a requested block."""
        if not self.connected or not self.writer:
            return
        try:
            msg = struct.pack(">IBIII", 13, 8, piece_index, begin, length)
            self.writer.write(msg)
            await self.writer.drain()
        except Exception:
            self.connected = False
    
    async def receive_message(self, timeout=None):
        """Receive and parse a message from peer."""
        if not self.connected or not self.reader:
            return None, None
        
        t = timeout if timeout is not None else self.timeout
        try:
            # Read message length (4 bytes)
            length_data = await asyncio.wait_for(
                self.reader.readexactly(4),
                timeout=t
            )
            length = struct.unpack(">I", length_data)[0]
            
            if length == 0:
                # Keep-alive
                return None, None
            
            if length > 2 * 1024 * 1024:
                # Malformed / abnormally large message
                self.connected = False
                return None, None
            
            # Read message ID and payload
            msg_data = await asyncio.wait_for(
                self.reader.readexactly(length),
                timeout=t
            )
            msg_id = msg_data[0]
            payload = msg_data[1:] if length > 1 else b''
            
            return msg_id, payload
            
        except asyncio.TimeoutError:
            return None, None
        except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError, OSError):
            self.connected = False
            return None, None
        except Exception:
            self.connected = False
            return None, None
    
    def handle_message(self, msg_id, payload):
        """Process received messages."""
        if msg_id is None:
            return None
        
        if msg_id == 0:
            self.peer_choking = True
        elif msg_id == 1:
            self.peer_choking = False
        elif msg_id == 2:
            self.peer_interested = True
        elif msg_id == 3:
            self.peer_interested = False
        elif msg_id == 4:
            piece_index = struct.unpack(">I", payload)[0]
            self.have_pieces.add(piece_index)
        elif msg_id == 5:
            self.bitfield = payload
            for byte_idx, byte_val in enumerate(payload):
                for bit_idx in range(8):
                    if (byte_val >> (7 - bit_idx)) & 1:
                        self.have_pieces.add(byte_idx * 8 + bit_idx)
        elif msg_id == 14:
            # Fast Extension: Have All
            self.has_all = True
        elif msg_id == 7:
            index = struct.unpack(">I", payload[0:4])[0]
            begin = struct.unpack(">I", payload[4:8])[0]
            block = payload[8:]
            return ('piece', index, begin, block)
        
        return None
    
    def has_piece(self, piece_index):
        """Check if peer has a specific piece."""
        if self.has_all:
            return True
        return piece_index in self.have_pieces
    
    async def close(self):
        """Close connection to peer."""
        self.connected = False
        if self.writer:
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except Exception:
                pass
            self.writer = None
            self.reader = None


async def download_piece_from_peer(peer, piece_index, piece_length, downloader=None, block_size=16384):
    """
    Download a single piece from a peer.
    Returns dict of {offset: block_data} or None if failed.
    """
    if not peer.has_piece(piece_index) or not peer.connected:
        return None
    
    # Send interested
    if not peer.interested:
        await peer.send_interested()
    
    # Wait for unchoke with timeout
    wait_time = 0.0
    while peer.peer_choking and wait_time < 5.0 and peer.connected:
        if downloader and piece_index in downloader.downloaded_pieces:
            return None
        msg_id, payload = await peer.receive_message(timeout=1.0)
        if msg_id is not None:
            peer.handle_message(msg_id, payload)
        wait_time += 1.0
    
    if peer.peer_choking or not peer.connected:
        return None
    
    # Request all blocks
    blocks_needed = []
    for begin in range(0, piece_length, block_size):
        length = min(block_size, piece_length - begin)
        blocks_needed.append((begin, length))
        await peer.send_request(piece_index, begin, length)
    
    # Collect blocks
    piece_data = {}
    timeout_counter = 0
    max_timeouts = 5  # 5 * 2.0s = 10s max stall without receiving blocks
    
    while len(piece_data) < len(blocks_needed) and timeout_counter < max_timeouts and peer.connected:
        if peer.peer_choking:
            # Choked midway through download
            break
        
        if downloader and piece_index in downloader.downloaded_pieces:
            # Piece already completed by another peer (endgame mode)
            return None
        
        msg_id, payload = await peer.receive_message(timeout=2.0)
        
        if msg_id is None:
            timeout_counter += 1
            # Re-request missing blocks if stalled for 4s
            if timeout_counter in (2, 4) and peer.connected and not peer.peer_choking:
                for begin, length in blocks_needed:
                    if begin not in piece_data:
                        await peer.send_request(piece_index, begin, length)
            continue
        
        result = peer.handle_message(msg_id, payload)
        if result and result[0] == 'piece':
            _, idx, begin, block = result
            if idx == piece_index:
                piece_data[begin] = block
                timeout_counter = 0
    
    if len(piece_data) < len(blocks_needed):
        return None
    
    return piece_data


class TorrentDownloader:
    """Manages concurrent downloading from multiple peers with Endgame Mode and dynamic peer pool."""
    
    def __init__(self, torrent_file_path, peers, max_peers=5, on_progress=None):
        self.torrent_file_path = torrent_file_path
        # Deduplicate peers
        self.peers = list(dict.fromkeys(peers))
        self.max_peers = max_peers
        self.on_progress = on_progress
        self.cancelled = False
        
        # Load torrent metadata
        with open(torrent_file_path, 'rb') as f:
            torrent_data = bdecode(f.read())
        
        self.info = torrent_data[b'info']
        self.info_hash = hashlib.sha1(bencode(self.info)).digest()
        self.piece_length = self.info[b'piece length']
        self.pieces_hash = self.info[b'pieces']
        self.num_pieces = len(self.pieces_hash) // 20
        
        # Calculate total length
        if b'length' in self.info:
            self.total_length = self.info[b'length']
        else:
            self.total_length = sum(f[b'length'] for f in self.info[b'files'])
        
        # Generate peer_id
        self.peer_id = b'-PY0001-' + os.urandom(12)
        
        # Piece management
        self.downloaded_pieces = {}
        self.piece_locks = {i: asyncio.Lock() for i in range(self.num_pieces)}
        self.pieces_in_progress = set()
        self.piece_downloaders = defaultdict(int)  # piece_idx -> count of active workers downloading it
        self.failed_pieces = defaultdict(dict)     # (ip, port) -> {piece_idx: failed_timestamp}
        self.connected_peers = []
        self.bytes_downloaded = 0
        
        # Track per-peer stats for UI
        self.peer_stats = {}  # {(ip, port): {"status": str, "pieces_downloaded": int}}
        
        print(f"Torrent: {self.num_pieces} pieces, {self.total_length} bytes total")
    
    def _notify_progress(self, message=""):
        """Send progress update via callback if one is registered."""
        if self.on_progress:
            peer_info = [
                {
                    "ip": ip,
                    "port": port,
                    "status": stats.get("status", "Unknown"),
                    "pieces_downloaded": stats.get("pieces_downloaded", 0),
                }
                for (ip, port), stats in list(self.peer_stats.items())
            ]
            self.on_progress({
                "downloaded_pieces": len(self.downloaded_pieces),
                "total_pieces": self.num_pieces,
                "connected_peers": peer_info,
                "message": message,
                "bytes_downloaded": self.bytes_downloaded,
                "total_bytes": self.total_length,
            })
    
    def get_piece_length(self, piece_idx):
        """Get the length of a specific piece."""
        if piece_idx == self.num_pieces - 1:
            return self.total_length - (piece_idx * self.piece_length)
        return self.piece_length
    
    def get_piece_hash(self, piece_idx):
        """Get the expected 20-byte SHA1 hash of a piece."""
        return self.pieces_hash[piece_idx * 20:(piece_idx + 1) * 20]
    
    def verify_piece(self, piece_idx, piece_data):
        """Verify a piece's hash."""
        calculated_hash = hashlib.sha1(piece_data).digest()
        expected_hash = self.get_piece_hash(piece_idx)
        return calculated_hash == expected_hash

    def choose_piece(self, peer, ip, port):
        """
        Choose the best piece for this peer to download.
        Supports Normal Mode (Rarest-first) and Endgame Mode (when last few pieces remain).
        """
        now = time.time()
        peer_failures = self.failed_pieces.get((ip, port), {})
        
        # Candidate pieces that this peer has and we still need
        remaining = [
            i for i in range(self.num_pieces)
            if i not in self.downloaded_pieces and peer.has_piece(i)
        ]
        if not remaining:
            return None
        
        # Endgame mode triggers when:
        # 1. Total uncompleted pieces <= 5, OR
        # 2. All remaining pieces are already in progress by at least one worker
        uncompleted_count = self.num_pieces - len(self.downloaded_pieces)
        is_endgame = (uncompleted_count <= 5) or all(
            i in self.pieces_in_progress for i in remaining
        )
        
        if is_endgame:
            # In endgame mode: allow multiple peers to download remaining pieces in parallel!
            # Filter out pieces that recently failed on this specific peer (cooldown: 5s)
            candidates = [
                i for i in remaining
                if (now - peer_failures.get(i, 0)) > 5
            ]
            if not candidates:
                candidates = remaining
            
            # Prioritize pieces with the fewest concurrent downloaders, random tie-breaker
            candidates.sort(key=lambda idx: (self.piece_downloaders[idx], random.random()))
            return candidates[0]
        else:
            # Normal mode: only select pieces not currently in progress
            available = [
                i for i in remaining
                if i not in self.pieces_in_progress
                and (now - peer_failures.get(i, 0)) > 20
            ]
            if not available:
                # If all available pieces failed recently, allow retry after shorter 5s cooldown
                available = [
                    i for i in remaining
                    if i not in self.pieces_in_progress
                    and (now - peer_failures.get(i, 0)) > 5
                ]
            if not available:
                return None
            
            # Rarest-first piece selection:
            # Count peer availability for each candidate piece across connected peers
            rarity = {}
            for i in available:
                rarity[i] = sum(1 for p in self.connected_peers if p.has_piece(i))
            
            # Sort rarest first; tie-break randomly so peers don't contend on same piece
            available.sort(key=lambda idx: (rarity.get(idx, 0), random.random()))
            return available[0]
    
    async def peer_worker(self, ip, port):
        """Worker coroutine for a single peer."""
        peer = AsyncBitTorrentPeer(ip, port, self.info_hash, self.peer_id)
        self.peer_stats[(ip, port)] = {"status": "Connecting", "pieces_downloaded": 0}
        self._notify_progress(f"Connecting to {ip}:{port}...")
        
        # Connect and handshake
        if not await peer.connect():
            self.peer_stats[(ip, port)]["status"] = "Failed"
            self._notify_progress(f"✗ Failed to connect to {ip}:{port}")
            return
        
        if not await peer.handshake():
            self.peer_stats[(ip, port)]["status"] = "Failed"
            self._notify_progress(f"✗ Handshake failed with {ip}:{port}")
            await peer.close()
            return
        
        self.peer_stats[(ip, port)]["status"] = "Connected"
        self._notify_progress(f"✓ Handshake successful with {ip}:{port}")
        
        # Initial wait for bitfield / have messages
        for _ in range(15):
            msg_id, payload = await peer.receive_message(timeout=0.2)
            if msg_id is not None:
                peer.handle_message(msg_id, payload)
            if peer.bitfield is not None or peer.have_pieces or peer.has_all:
                break
        
        self.connected_peers.append(peer)
        
        try:
            # Download loop
            while len(self.downloaded_pieces) < self.num_pieces and not self.cancelled and peer.connected:
                # Find a piece to download
                piece_idx = self.choose_piece(peer, ip, port)
                
                if piece_idx is None:
                    # No pieces available right now. Keep socket active by reading incoming messages
                    try:
                        msg_id, payload = await peer.receive_message(timeout=1.0)
                        if msg_id is not None:
                            peer.handle_message(msg_id, payload)
                    except Exception:
                        break
                    continue
                
                # Mark piece in progress
                self.pieces_in_progress.add(piece_idx)
                self.piece_downloaders[piece_idx] += 1
                self.peer_stats[(ip, port)]["status"] = f"Downloading #{piece_idx}"
                
                try:
                    piece_length = self.get_piece_length(piece_idx)
                    piece_blocks = await download_piece_from_peer(peer, piece_idx, piece_length, downloader=self)
                    
                    if piece_blocks and piece_idx not in self.downloaded_pieces:
                        # Assemble piece
                        complete_piece = b''.join(piece_blocks[offset] for offset in sorted(piece_blocks.keys()))
                        
                        # Verify
                        if self.verify_piece(piece_idx, complete_piece):
                            async with self.piece_locks[piece_idx]:
                                if piece_idx not in self.downloaded_pieces:
                                    self.downloaded_pieces[piece_idx] = complete_piece
                                    self.bytes_downloaded += len(complete_piece)
                            
                            self.peer_stats[(ip, port)]["pieces_downloaded"] += 1
                            self.peer_stats[(ip, port)]["status"] = "Unchoked"
                            progress = len(self.downloaded_pieces)
                            print(f"✓ Piece {piece_idx} downloaded from {ip}:{port} ({progress}/{self.num_pieces})")
                            self._notify_progress(f"✓ Piece {piece_idx} downloaded from {ip}:{port} ({progress}/{self.num_pieces})")
                        else:
                            print(f"✗ Piece {piece_idx} failed verification from {ip}:{port}")
                            self._notify_progress(f"✗ Piece {piece_idx} failed verification from {ip}:{port}")
                            self.failed_pieces[(ip, port)][piece_idx] = time.time()
                    else:
                        if piece_idx not in self.downloaded_pieces:
                            self.failed_pieces[(ip, port)][piece_idx] = time.time()
                finally:
                    self.piece_downloaders[piece_idx] = max(0, self.piece_downloaders[piece_idx] - 1)
                    if self.piece_downloaders[piece_idx] == 0:
                        self.pieces_in_progress.discard(piece_idx)
                
                # Yield control briefly
                await asyncio.sleep(0.01)
        
        except Exception as e:
            print(f"Error in peer worker {ip}:{port}: {e}")
            self._notify_progress(f"✗ Error with {ip}:{port}: {e}")
        finally:
            self.peer_stats[(ip, port)]["status"] = "Disconnected"
            self._notify_progress(f"Peer {ip}:{port} disconnected")
            await peer.close()
            if peer in self.connected_peers:
                self.connected_peers.remove(peer)
    
    async def download(self, output_file):
        """Start concurrent download from multiple peers with dynamic peer pool and endgame strategy."""
        self._notify_progress("Starting download...")
        
        peer_queue = list(self.peers)
        random.shuffle(peer_queue)
        tried_peers = set()
        active_tasks = {}  # task -> (ip, port)
        
        last_progress_time = time.time()
        last_downloaded_count = 0
        
        try:
            while len(self.downloaded_pieces) < self.num_pieces and not self.cancelled:
                # Clean up finished tasks
                done_tasks = [t for t in active_tasks if t.done()]
                for t in done_tasks:
                    del active_tasks[t]
                
                # Launch new peer workers up to max_peers
                while len(active_tasks) < self.max_peers and peer_queue and not self.cancelled:
                    ip, port = peer_queue.pop(0)
                    tried_peers.add((ip, port))
                    task = asyncio.create_task(self.peer_worker(ip, port))
                    active_tasks[task] = (ip, port)
                    await asyncio.sleep(0.05)
                
                # Track progress
                current_downloaded = len(self.downloaded_pieces)
                if current_downloaded > last_downloaded_count:
                    last_downloaded_count = current_downloaded
                    last_progress_time = time.time()
                
                # If stalled for 15s or out of peers, replenish peer queue from tracker or retry known peers
                if (not peer_queue and len(active_tasks) < max(3, self.max_peers // 3)) or (time.time() - last_progress_time > 15):
                    try:
                        from get_peers import get_peers_from_tracker
                        fresh_peers = get_peers_from_tracker(self.torrent_file_path)
                        for p in fresh_peers:
                            if p not in tried_peers and p not in peer_queue:
                                peer_queue.append(p)
                                if p not in self.peers:
                                    self.peers.append(p)
                    except Exception:
                        pass
                    
                    # If still no peers in queue and few active tasks, allow retrying known peers
                    if not peer_queue and len(active_tasks) <= 2:
                        retryable = [p for p in self.peers if p not in active_tasks.values()]
                        random.shuffle(retryable)
                        peer_queue.extend(retryable)
                        tried_peers.clear()
                    
                    last_progress_time = time.time()
                
                if not active_tasks and not peer_queue:
                    # No active tasks and no peers left to try
                    break
                
                await asyncio.sleep(0.5)
        
        finally:
            # Cancel all running tasks
            for task in list(active_tasks.keys()):
                if not task.done():
                    task.cancel()
            if active_tasks:
                await asyncio.gather(*active_tasks.keys(), return_exceptions=True)
        
        # Write to file if complete
        if len(self.downloaded_pieces) == self.num_pieces:
            self._notify_progress("Download complete! Writing to file...")
            print("\nDownload complete! Writing to file...")
            
            if b'files' in self.info:
                # Multi-file torrent
                base_dir = output_file
                os.makedirs(base_dir, exist_ok=True)
                
                file_idx = 0
                file_offset = 0
                current_file = None
                
                try:
                    for piece_idx in range(self.num_pieces):
                        piece_bytes = self.downloaded_pieces[piece_idx]
                        piece_pos = 0
                        piece_len = len(piece_bytes)
                        
                        while piece_pos < piece_len and file_idx < len(self.info[b'files']):
                            file_info = self.info[b'files'][file_idx]
                            file_length = file_info[b'length']
                            file_rel_path = os.path.join(*[p.decode('utf-8', errors='replace') for p in file_info[b'path']])
                            full_path = os.path.join(base_dir, file_rel_path)
                            
                            if current_file is None:
                                os.makedirs(os.path.dirname(full_path), exist_ok=True)
                                current_file = open(full_path, 'wb')
                            
                            bytes_needed = file_length - file_offset
                            bytes_available = piece_len - piece_pos
                            bytes_to_write = min(bytes_needed, bytes_available)
                            
                            current_file.write(piece_bytes[piece_pos:piece_pos + bytes_to_write])
                            piece_pos += bytes_to_write
                            file_offset += bytes_to_write
                            
                            if file_offset >= file_length:
                                current_file.close()
                                current_file = None
                                file_idx += 1
                                file_offset = 0
                finally:
                    if current_file:
                        current_file.close()
            else:
                # Single-file torrent
                parent_dir = os.path.dirname(os.path.abspath(output_file))
                if parent_dir:
                    os.makedirs(parent_dir, exist_ok=True)
                with open(output_file, 'wb') as f:
                    for i in range(self.num_pieces):
                        f.write(self.downloaded_pieces[i])
            
            print(f"✓ File saved to {output_file}")
            self._notify_progress(f"✓ File saved to {output_file}")
            return True
        else:
            msg = f"✗ Download incomplete: {len(self.downloaded_pieces)}/{self.num_pieces} pieces"
            print(f"\n{msg}")
            self._notify_progress(msg)
            return False


async def download_from_peers_async(torrent_file, peers, output_file, max_peers=5, on_progress=None):
    downloader = TorrentDownloader(torrent_file, peers, max_peers, on_progress=on_progress)
    success = await downloader.download(output_file)
    return downloader, success
