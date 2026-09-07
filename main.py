import asyncio
    
async def main():
    peers = get_peers('X.torrent')
    if len(peers) == 0:
        print("No peers found in tracker")
        return

    success = await download_from_peers(
        'x.torrent',
        peers,
        'file.mkv',
        max_peers=50
    )

    if success:
        print("Download successful!")
    else:
        print("Download failed or incomplete")

try:
    asyncio.run(main())
except KeyboardInterrupt:
    print("\nDownload interrupted by user")

def get_peers(file):
    print("trackers")

def download_from_peers(file):
    print("download")