

import os
from pathlib import Path
import hashlib
from textual.app import App, ComposeResult
from textual.containers import Vertical, Horizontal
from textual.widgets import (
    Header, Footer, Input, Button, ProgressBar,
    DataTable, RichLog, Static,
)
from textual import work
from textual.binding import Binding

from parser import bdecode, bencode
from get_peers import get_peers_from_tracker
from connect_to_peers_async import TorrentDownloader


def format_bytes(num_bytes):
    """Format bytes into a human-readable string."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num_bytes) < 1024:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} PB"


def parse_torrent_info(torrent_file_path):
    """Parse a .torrent file and return metadata dict."""
    with open(torrent_file_path, "rb") as f:
        torrent_data = f.read()

    decoded = bdecode(torrent_data)
    info = decoded[b"info"]

    # Name
    name = info.get(b"name", b"Unknown").decode("utf-8", errors="replace")

    # Tracker
    tracker = decoded.get(b"announce", b"Unknown").decode("utf-8", errors="replace")

    # Piece info
    piece_length = info[b"piece length"]
    pieces_hash = info[b"pieces"]
    num_pieces = len(pieces_hash) // 20

    # Total size
    if b"length" in info:
        total_length = info[b"length"]
    else:
        total_length = sum(f[b"length"] for f in info[b"files"])

    # Info hash
    info_hash = hashlib.sha1(bencode(info)).hexdigest()

    return {
        "name": name,
        "tracker": tracker,
        "piece_length": piece_length,
        "num_pieces": num_pieces,
        "total_length": total_length,
        "info_hash": info_hash,
    }


class TorrentApp(App):
    """A Textual TUI for the BitTorrent client."""

    CSS_PATH = "app.tcss"
    TITLE = "Torrent Client"

    BINDINGS = [
        Binding("q", "quit", "Quit", show=True),
        Binding("s", "start_download", "Start", show=True),
        Binding("x", "stop_download", "Stop", show=True),
    ]

    def __init__(self):
        super().__init__()
        self.downloader = None
        self.torrent_path = None
        self.torrent_info = None
        self._downloading = False

    def compose(self) -> ComposeResult:
        yield Header()

        with Vertical(id="app-grid"):
            # ── Controls ──
            with Horizontal(id="controls"):
                yield Input(
                    placeholder="Path to .torrent file...",
                    id="torrent-input",
                )
                yield Button("Load", id="btn-load", variant="primary")
                yield Button("Start", id="btn-start", variant="success")
                yield Button("Stop", id="btn-stop", variant="error")

            # ── Torrent Info ──
            with Vertical(id="info-panel"):
                yield Static("No torrent loaded", id="info-content")

            # ── Progress ──
            with Horizontal(id="progress-section"):
                yield ProgressBar(total=100, id="progress-bar", show_eta=False)
                yield Static("0/0 pieces  |  0 peers", id="progress-label")

            # ── Peer Table ──
            with Vertical(id="peer-section"):
                yield Static("Peers", id="peer-title")
                yield DataTable(id="peer-table")

            # ── Log ──
            with Vertical(id="log-section"):
                yield Static("Activity Log", id="log-title")
                yield RichLog(
                    highlight=True,
                    markup=True,
                    auto_scroll=True,
                    id="activity-log",
                )

        yield Footer()

    def on_mount(self) -> None:
        """Set up the peer table columns."""
        table = self.query_one("#peer-table", DataTable)
        table.add_columns("IP", "Port", "Status", "Pieces DL'd")
        table.cursor_type = "row"

        log = self.query_one("#activity-log", RichLog)
        log.write("[bold cyan]Welcome to Torrent Client[/]")
        log.write("Enter a .torrent file path and click [bold]Load[/]")

        # Auto-detect .torrent files in the current directory
        torrent_files = [f for f in os.listdir(".") if f.endswith(".torrent")]
        if torrent_files:
            log.write(
                f"[dim]Found {len(torrent_files)} .torrent file(s) in current directory: "
                f"{', '.join(torrent_files)}[/]"
            )

    # ── Button handlers ──

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-load":
            self._load_torrent()
        elif event.button.id == "btn-start":
            self.action_start_download()
        elif event.button.id == "btn-stop":
            self.action_stop_download()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Handle Enter key in the input field."""
        if event.input.id == "torrent-input":
            self._load_torrent()

    # ── Load torrent ──

    def _load_torrent(self) -> None:
        """Load and display torrent metadata."""
        log = self.query_one("#activity-log", RichLog)
        torrent_input = self.query_one("#torrent-input", Input)
        path = torrent_input.value.strip().strip('"').strip("'")

        if not path:
            log.write("[bold red]Error:[/] Please enter a .torrent file path")
            return

        if not os.path.isfile(path):
            log.write(f"[bold red]Error:[/] File not found: {path}")
            return

        try:
            self.torrent_info = parse_torrent_info(path)
            self.torrent_path = path
        except Exception as e:
            log.write(f"[bold red]Error parsing torrent:[/] {e}")
            return

        info = self.torrent_info
        info_text = (
            f"[bold]Name:[/]    {info['name']}\n"
            f"[bold]Size:[/]    {format_bytes(info['total_length'])}\n"
            f"[bold]Pieces:[/]  {info['num_pieces']}  "
            f"({format_bytes(info['piece_length'])} each)\n"
            f"[bold]Tracker:[/] {info['tracker']}\n"
            f"[bold]Hash:[/]    {info['info_hash']}"
        )

        info_widget = self.query_one("#info-content", Static)
        info_widget.update(info_text)

        # Reset progress
        progress_bar = self.query_one("#progress-bar", ProgressBar)
        progress_bar.update(total=info["num_pieces"], progress=0)
        self.query_one("#progress-label", Static).update(
            f"0/{info['num_pieces']} pieces  |  0 peers"
        )

        log.write(f"[bold green]✓[/] Loaded: [bold]{info['name']}[/]")
        log.write(
            f"  {format_bytes(info['total_length'])} across "
            f"{info['num_pieces']} pieces"
        )

    # ── Download ──

    def action_start_download(self) -> None:
        """Start the download."""
        log = self.query_one("#activity-log", RichLog)

        if self._downloading:
            log.write("[yellow]Download already in progress[/]")
            return

        if not self.torrent_path or not self.torrent_info:
            log.write("[bold red]Error:[/] Load a .torrent file first")
            return

        self._downloading = True
        log.write("[bold cyan]Starting download...[/]")
        self._run_download()

    @work(exclusive=True, thread=True)
    def _run_download(self) -> None:
        """Run the download in a background thread using Textual workers."""
        import asyncio

        log = self.query_one("#activity-log", RichLog)

        # Get peers from tracker
        try:
            self.call_from_thread(
                log.write, "[dim]Contacting tracker...[/]"
            )
            peers = get_peers_from_tracker(self.torrent_path)
        except Exception as e:
            self.call_from_thread(
                log.write, f"[bold red]Tracker error:[/] {e}"
            )
            self._downloading = False
            return

        if not peers:
            self.call_from_thread(
                log.write, "[bold red]No peers found from tracker[/]"
            )
            self._downloading = False
            return

        self.call_from_thread(
            log.write, f"[bold green]✓[/] Found [bold]{len(peers)}[/] peers"
        )

        # Progress callback — called from the async download loop
        def on_progress(data):
            try:
                self.call_from_thread(self._update_ui, data)
            except Exception:
                pass  # App may be closing

        # Run the async download in a new event loop (since we're in a thread)
        loop = asyncio.new_event_loop()
        try:
            self.downloader = TorrentDownloader(
                self.torrent_path,
                peers,
                max_peers=50,
                on_progress=on_progress,
            )

            downloads_dir = Path.home() / "Downloads"
            downloads_dir.mkdir(parents=True, exist_ok=True)
            output_path = str(downloads_dir / self.torrent_info["name"])

            self.call_from_thread(
                log.write, f"[dim]Saving to: {output_path}[/]"
            )

            success = loop.run_until_complete(
                self.downloader.download(output_path)
            )

            if success:
                self.call_from_thread(
                    log.write,
                    f"[bold green]✓ Download complete! Saved to {output_path}[/]",
                )
            else:
                if self.downloader.cancelled:
                    self.call_from_thread(
                        log.write, "[yellow]Download cancelled[/]"
                    )
                else:
                    self.call_from_thread(
                        log.write, "[bold red]✗ Download incomplete[/]"
                    )
        except Exception as e:
            self.call_from_thread(
                log.write, f"[bold red]Download error:[/] {e}"
            )
        finally:
            loop.close()
            self._downloading = False
            self.downloader = None

    def _update_ui(self, data: dict) -> None:
        """Update all UI widgets from a progress dict. Runs on the main thread."""
        # Update log
        message = data.get("message", "")
        if message:
            log = self.query_one("#activity-log", RichLog)
            # Color-code messages
            if message.startswith("✓"):
                log.write(f"[green]{message}[/]")
            elif message.startswith("✗"):
                log.write(f"[red]{message}[/]")
            else:
                log.write(f"[dim]{message}[/]")

        # Update progress bar
        downloaded = data.get("downloaded_pieces", 0)
        total = data.get("total_pieces", 1)
        progress_bar = self.query_one("#progress-bar", ProgressBar)
        progress_bar.update(total=total, progress=downloaded)

        # Update progress label
        num_peers = len(data.get("connected_peers", []))
        active_peers = sum(
            1 for p in data.get("connected_peers", [])
            if p["status"] not in ("Disconnected", "Failed")
        )
        bytes_dl = data.get("bytes_downloaded", 0)
        total_bytes = data.get("total_bytes", 0)

        pct = (downloaded / total * 100) if total > 0 else 0
        label = (
            f"{downloaded}/{total} pieces ({pct:.1f}%)  |  "
            f"{format_bytes(bytes_dl)}/{format_bytes(total_bytes)}  |  "
            f"{active_peers} peers"
        )
        self.query_one("#progress-label", Static).update(label)

        # Update peer table
        peer_list = data.get("connected_peers", [])
        table = self.query_one("#peer-table", DataTable)
        table.clear()
        for peer in peer_list:
            status = peer["status"]
            # Highlight active statuses
            table.add_row(
                peer["ip"],
                str(peer["port"]),
                status,
                str(peer["pieces_downloaded"]),
            )

    def action_stop_download(self) -> None:
        """Stop the current download."""
        log = self.query_one("#activity-log", RichLog)

        if not self._downloading or not self.downloader:
            log.write("[yellow]No download in progress[/]")
            return

        log.write("[bold yellow]Cancelling download...[/]")
        self.downloader.cancelled = True


if __name__ == "__main__":
    app = TorrentApp()
    app.run()
