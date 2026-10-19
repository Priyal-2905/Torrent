import asyncio
from pathlib import Path
from get_peers import get_peers_from_tracker
from connect_to_peers_async import download_from_peers_async

async def main():    
    #  Get peers
    peers = get_peers_from_tracker('one-piece.torrent')
    if len(peers) == 0:
        print("No peers found in tracker. Exiting...")
        return
    
    output_path = str(Path.home() / "Downloads" / "downloaded_file.mkv")
    print(f"Downloading to: {output_path}")

    # Connect with peers and start downloading
    downloader, success = await download_from_peers_async(
        'one-piece.torrent',
        peers,
        output_path,
        max_peers=50
    )
    
    if success:
        print("Download successful!")
    else:
        print("Download failed or incomplete")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nDownload interrupted by user")